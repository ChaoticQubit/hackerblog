#!/usr/bin/env bash
set -euo pipefail

readonly app_user=hackerblog
readonly app_home=/home/hackerblog
readonly app_dir=/home/hackerblog/app
readonly hb_home=/home/hackerblog/.hackerblog
readonly output_dir=/home/hackerblog/HackerBlog
readonly uv_bin=/home/hackerblog/.local/bin/uv
readonly uv_version=0.13.5
readonly lib_dir=/usr/local/lib/hackerblog
readonly backup_dir=/mnt/hackerblog-backup
readonly web_auth_file=/etc/caddy/hackerblog-web-auth
readonly caddyfile=/etc/caddy/Caddyfile
readonly sshd_dropin=/etc/ssh/sshd_config.d/10-hackerblog.conf
readonly health_url=http://127.0.0.1:8765/v1/health
readonly units=(hackerblog.service hackerblog-keepalive.service hackerblog-backup.service hackerblog-backup.timer)

domain=""
email=""
repo=""
branch=main
web_user=owner
dry_run=0

usage() {
	cat <<'EOF'
Usage: sudo ./setup.sh --domain NAME --email ADDRESS --repo GIT_URL [options]

Sets up HackerBlog on an Ubuntu 24.04 Arm VM: packages, the hackerblog user, uv, the app,
systemd units, Caddy with HTTPS and basic auth, the host firewall and key-only SSH.
Safe to run again; each step skips what is already in place.

Options:
  --domain NAME     public host name, for example hb-owner.duckdns.org
  --email ADDRESS   contact address for Let's Encrypt expiry notices
  --repo GIT_URL    the HackerBlog git repository to install from
  --branch NAME     branch to check out (default: main)
  --web-user NAME   browser basic-auth user name (default: owner)
  --dry-run         print every change instead of making it
  -h, --help        show this help

The browser password is read from HACKERBLOG_WEB_PASSWORD, or prompted for, the first time.
EOF
}

die() {
	printf 'setup: error: %s\n' "$1" >&2
	exit 1
}

say() {
	printf '==> %s\n' "$1"
}

warn() {
	printf 'setup: warning: %s\n' "$1" >&2
}

run() {
	if ((dry_run)); then
		printf 'would run:'
		printf ' %q' "$@"
		printf '\n'
	else
		"$@"
	fi
}

as_app() {
	run sudo -u "$app_user" -H -- "$@"
}

write_file() {
	local path="$1" mode="$2" owner="$3" tmp
	if ((dry_run)); then
		cat >/dev/null
		printf 'would write: %s (mode %s, owner %s)\n' "$path" "$mode" "$owner"
		return
	fi
	tmp="$(mktemp "${path}.XXXXXX")"
	cat >"$tmp"
	chmod "$mode" "$tmp"
	chown "$owner" "$tmp"
	mv -f "$tmp" "$path"
}

is_mounted() {
	command -v mountpoint >/dev/null 2>&1 && mountpoint -q "$1"
}

parse_args() {
	while (($#)); do
		case "$1" in
		--domain) domain="${2:-}"; shift 2 ;;
		--email) email="${2:-}"; shift 2 ;;
		--repo) repo="${2:-}"; shift 2 ;;
		--branch) branch="${2:-}"; shift 2 ;;
		--web-user) web_user="${2:-}"; shift 2 ;;
		--dry-run) dry_run=1; shift ;;
		-h | --help) usage; exit 0 ;;
		*) usage >&2; die "unknown argument: $1" ;;
		esac
	done
	[[ "$domain" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] ||
		die "--domain must be a DNS name like hb-owner.duckdns.org, got '$domain'"
	[[ "$email" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] ||
		die "--email must be an e-mail address, got '$email'"
	[[ -n "$repo" && "$repo" != -* ]] || die "--repo is required"
	[[ "$branch" =~ ^[A-Za-z0-9._/-]+$ && "$branch" != -* ]] || die "--branch has unsafe characters: '$branch'"
	[[ "$web_user" =~ ^[A-Za-z0-9._-]+$ ]] || die "--web-user may use letters, digits, '.', '_' and '-' only"
}

check_platform() {
	if ((dry_run)); then
		say "Dry run: nothing will be changed"
	else
		[[ "$(id -u)" == 0 ]] || die "run as root: sudo $0 ..."
	fi
	local os_id=""
	if [[ -r /etc/os-release ]]; then
		os_id="$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')"
	fi
	if [[ "$os_id" != ubuntu ]]; then
		((dry_run)) || die "this script supports Ubuntu only (found '${os_id:-unknown}')"
		warn "not Ubuntu (found '${os_id:-unknown}'); continuing only because of --dry-run"
	fi
	if [[ "$(uname -m)" != aarch64 && "$(uname -m)" != arm64 ]]; then
		warn "expected an Arm (aarch64) VM.Standard.A1.Flex instance, found $(uname -m)"
	fi
}

install_packages() {
	say "Packages"
	run env DEBIAN_FRONTEND=noninteractive apt-get update -q
	run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
		git sqlite3 curl ca-certificates gnupg python3 \
		debian-keyring debian-archive-keyring apt-transport-https \
		iptables netfilter-persistent iptables-persistent
	if command -v caddy >/dev/null 2>&1; then
		say "Caddy already installed: $(caddy version 2>/dev/null | head -n 1)"
		return
	fi
	local keyring=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
	local source_list=/etc/apt/sources.list.d/caddy-stable.list
	if ((dry_run)); then
		printf 'would write: %s from https://dl.cloudsmith.io/public/caddy/stable/gpg.key\n' "$keyring"
		printf 'would write: %s from https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt\n' "$source_list"
	else
		curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --batch --yes --dearmor -o "$keyring"
		chmod 0644 "$keyring"
		curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt |
			write_file "$source_list" 0644 root:root
	fi
	run env DEBIAN_FRONTEND=noninteractive apt-get update -q
	run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q caddy
}

create_user() {
	say "User $app_user"
	if id -u "$app_user" >/dev/null 2>&1; then
		say "User exists"
	else
		run useradd --create-home --home-dir "$app_home" --shell /bin/bash "$app_user"
	fi
	run chmod 0700 "$app_home"
}

install_uv() {
	say "uv $uv_version"
	if [[ -x "$uv_bin" ]] && [[ "$("$uv_bin" --version 2>/dev/null)" == "uv $uv_version"* ]]; then
		say "uv $uv_version already installed"
		return
	fi
	as_app sh -c "curl -LsSf https://astral.sh/uv/$uv_version/install.sh | env UV_NO_MODIFY_PATH=1 sh"
}

fetch_app() {
	say "App from $repo ($branch)"
	if [[ -d "$app_dir/.git" ]]; then
		say "Already cloned; use update.sh to pull new commits"
		return
	fi
	if [[ "$repo" == git@* || "$repo" == ssh://* ]]; then
		if [[ ! -f "$app_home/.ssh/id_ed25519" ]]; then
			as_app mkdir -p -m 0700 "$app_home/.ssh"
			as_app ssh-keygen -q -t ed25519 -N "" -C "hackerblog-deploy" -f "$app_home/.ssh/id_ed25519"
		fi
	fi
	if ! as_app env GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" \
		git clone --branch "$branch" -- "$repo" "$app_dir"; then
		if [[ -f "$app_home/.ssh/id_ed25519.pub" ]]; then
			printf '\nAdd this read-only deploy key to the repository, then run setup.sh again:\n\n' >&2
			cat "$app_home/.ssh/id_ed25519.pub" >&2
		fi
		die "git clone failed"
	fi
}

install_app() {
	say "Python environment and HackerBlog home"
	as_app "$uv_bin" --directory "$app_dir" sync --locked --no-dev
	as_app env HACKERBLOG_HOME="$hb_home" "$app_dir/.venv/bin/hackerblog" init --output-dir "$output_dir"
	run chmod 0600 "$hb_home/secrets.env"
}

prepare_backup_dir() {
	say "Backup volume at $backup_dir"
	run install -d -m 0755 "$backup_dir"
	if is_mounted "$backup_dir"; then
		run chown "$app_user:$app_user" "$backup_dir"
		run chmod 0700 "$backup_dir"
	else
		warn "$backup_dir is not mounted yet; nightly backups fail until the block volume is attached (guide step 5)"
	fi
}

install_units() {
	say "systemd units"
	local unit
	run install -d -m 0755 "$lib_dir"
	run install -m 0755 -o root -g root "$app_dir/deploy/backup.sh" "$lib_dir/backup.sh"
	run install -m 0644 -o root -g root "$app_dir/deploy/keepalive.py" "$lib_dir/keepalive.py"
	for unit in "${units[@]}"; do
		run install -m 0644 -o root -g root "$app_dir/deploy/$unit" "/etc/systemd/system/$unit"
	done
	run systemctl daemon-reload
	run systemctl enable --now hackerblog.service hackerblog-keepalive.service hackerblog-backup.timer
	run systemctl restart hackerblog.service
}

read_web_password() {
	local first="${HACKERBLOG_WEB_PASSWORD:-}" second
	if [[ -z "$first" ]]; then
		[[ -t 0 ]] || die "set HACKERBLOG_WEB_PASSWORD or run from a terminal to choose the browser password"
		read -rsp "Browser password for $web_user: " first
		printf '\n' >&2
		read -rsp "Same password again: " second
		printf '\n' >&2
		[[ "$first" == "$second" ]] || die "the passwords differ"
	fi
	((${#first} >= 12)) || die "use a browser password of at least 12 characters"
	printf '%s' "$first"
}

configure_caddy() {
	say "Caddy for https://$domain"
	if ((dry_run)); then
		printf 'would write: %s (basic-auth hash for %s, mode 0600)\n' "$web_auth_file" "$web_user"
		printf 'would write: %s from %s\n' "$caddyfile" "$app_dir/deploy/Caddyfile.template"
		run caddy validate --config "$caddyfile" --adapter caddyfile
		run systemctl enable caddy.service
		run systemctl reload-or-restart caddy.service
		return
	fi
	if [[ ! -s "$web_auth_file" ]] || [[ "$(cut -d ' ' -f 1 "$web_auth_file")" != "$web_user" ]]; then
		local password hash
		password="$(read_web_password)"
		hash="$(printf '%s\n' "$password" | caddy hash-password)"
		unset password
		[[ "$hash" == "\$2"* ]] || die "caddy hash-password did not return a bcrypt hash"
		printf '%s %s\n' "$web_user" "$hash" | write_file "$web_auth_file" 0600 root:root
	fi
	local user hash template
	read -r user hash <"$web_auth_file"
	template="$(<"$app_dir/deploy/Caddyfile.template")"
	template="${template//__ACME_EMAIL__/"$email"}"
	template="${template//__DOMAIN__/"$domain"}"
	template="${template//__WEB_USER__/"$user"}"
	template="${template//__WEB_HASH__/"$hash"}"
	local placeholder
	for placeholder in __ACME_EMAIL__ __DOMAIN__ __WEB_USER__ __WEB_HASH__; do
		[[ "$template" != *"$placeholder"* ]] || die "Caddyfile template still has $placeholder"
	done
	printf '%s\n' "$template" | write_file "$caddyfile" 0640 root:caddy
	caddy validate --config "$caddyfile" --adapter caddyfile
	systemctl enable caddy.service
	systemctl reload-or-restart caddy.service
}

open_firewall() {
	say "Host firewall: allow 80 and 443"
	if ((dry_run)) && ! command -v iptables >/dev/null 2>&1; then
		printf 'would run: iptables -I INPUT <before REJECT> -p tcp -m state --state NEW --dport {80,443} -j ACCEPT\n'
		printf 'would run: netfilter-persistent save\n'
		return
	fi
	if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
		die "ufw is active; Oracle advises against ufw on its Ubuntu images (guide troubleshooting)"
	fi
	local port position changed=0
	for port in 80 443; do
		if iptables -C INPUT -p tcp -m state --state NEW -m tcp --dport "$port" -j ACCEPT 2>/dev/null; then
			continue
		fi
		position="$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" { print $1; exit }')"
		if [[ -n "$position" ]]; then
			run iptables -I INPUT "$position" -p tcp -m state --state NEW -m tcp --dport "$port" -j ACCEPT
		else
			run iptables -A INPUT -p tcp -m state --state NEW -m tcp --dport "$port" -j ACCEPT
		fi
		changed=1
	done
	if ((changed)); then
		run netfilter-persistent save
	else
		say "Ports 80 and 443 already open"
	fi
}

harden_ssh() {
	say "SSH: keys only, no root login"
	printf '%s\n' "PasswordAuthentication no" "KbdInteractiveAuthentication no" \
		"PermitRootLogin no" "PubkeyAuthentication yes" |
		write_file "$sshd_dropin" 0644 root:root
	run sshd -t
	run systemctl try-reload-or-restart ssh.service
}

wait_healthy() {
	say "Waiting for $health_url"
	if ((dry_run)); then
		printf 'would run: curl -fsS %s\n' "$health_url"
		return
	fi
	local attempt
	for attempt in $(seq 1 30); do
		if curl -fsS "$health_url" >/dev/null 2>&1; then
			say "HackerBlog is up after ${attempt}s"
			return
		fi
		sleep 1
	done
	die "no answer from $health_url after 30s; see: journalctl -u hackerblog -n 50"
}

next_steps() {
	cat <<EOF

Done. Next:
  1. Put your Anthropic key in $hb_home/secrets.env (ANTHROPIC_API_KEY=...):
       sudo -u $app_user -H nano $hb_home/secrets.env
  2. Add sites and set [output] public_base_url = "https://$domain/files" in $hb_home/sites.toml.
  3. sudo systemctl restart hackerblog && sudo -u $app_user -H env HACKERBLOG_HOME=$hb_home $app_dir/.venv/bin/hackerblog check
  4. Open https://$domain/ and sign in as $web_user.
EOF
}

main() {
	parse_args "$@"
	check_platform
	install_packages
	create_user
	install_uv
	fetch_app
	install_app
	prepare_backup_dir
	install_units
	configure_caddy
	open_firewall
	harden_ssh
	wait_healthy
	next_steps
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

readonly app_user=hackerblog
readonly app_dir=/home/hackerblog/app
readonly uv_bin=/home/hackerblog/.local/bin/uv
readonly lib_dir=/usr/local/lib/hackerblog
readonly health_url=http://127.0.0.1:8765/v1/health
readonly units=(hackerblog.service hackerblog-keepalive.service hackerblog-backup.service hackerblog-backup.timer)

dry_run=0

die() {
	printf 'update: error: %s\n' "$1" >&2
	exit 1
}

say() {
	printf '==> %s\n' "$1"
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

main() {
	case "${1:-}" in
	"") ;;
	--dry-run) dry_run=1 ;;
	-h | --help)
		printf 'Usage: sudo %s [--dry-run]\nPull the checked-out branch, uv sync --locked, reinstall the units and restart.\n' "$0"
		exit 0
		;;
	*) die "unknown argument: $1" ;;
	esac
	if ((!dry_run)); then
		[[ "$(id -u)" == 0 ]] || die "run as root: sudo $0"
		[[ -d "$app_dir/.git" ]] || die "no app at $app_dir; run setup.sh first"
	fi

	say "Pull"
	as_app git -C "$app_dir" pull --ff-only
	say "Sync dependencies"
	as_app "$uv_bin" --directory "$app_dir" sync --locked --no-dev

	say "Reinstall units and helper scripts"
	local unit
	run install -m 0755 -o root -g root "$app_dir/deploy/backup.sh" "$lib_dir/backup.sh"
	run install -m 0644 -o root -g root "$app_dir/deploy/keepalive.py" "$lib_dir/keepalive.py"
	for unit in "${units[@]}"; do
		run install -m 0644 -o root -g root "$app_dir/deploy/$unit" "/etc/systemd/system/$unit"
	done
	run systemctl daemon-reload

	say "Restart"
	run systemctl restart hackerblog.service hackerblog-keepalive.service
	if ((dry_run)); then
		printf 'would run: curl -fsS %s\n' "$health_url"
		return
	fi
	local attempt
	for attempt in $(seq 1 30); do
		if curl -fsS "$health_url" >/dev/null 2>&1; then
			say "Healthy after ${attempt}s at $(sudo -u "$app_user" -H -- git -C "$app_dir" log -1 --format='%h %s')"
			return
		fi
		sleep 1
	done
	die "no answer from $health_url after 30s; see: journalctl -u hackerblog -n 50"
}

main "$@"

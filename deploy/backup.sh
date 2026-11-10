#!/usr/bin/env bash
set -euo pipefail

home_dir="${HACKERBLOG_HOME:-/home/hackerblog/.hackerblog}"
output_dir="${HACKERBLOG_OUTPUT_DIR:-/home/hackerblog/HackerBlog}"
backup_root="${HACKERBLOG_BACKUP_DIR:-/mnt/hackerblog-backup}"
keep="${HACKERBLOG_BACKUP_KEEP:-14}"
require_mount="${HACKERBLOG_BACKUP_REQUIRE_MOUNT:-1}"

die() {
	printf 'backup: %s\n' "$1" >&2
	exit 1
}

main() {
	[[ "$keep" =~ ^[1-9][0-9]*$ ]] || die "HACKERBLOG_BACKUP_KEEP must be a positive integer, got '$keep'"
	[[ -f "$home_dir/hackerblog.db" ]] || die "no database at $home_dir/hackerblog.db"
	[[ -d "$output_dir" ]] || die "no output folder at $output_dir"
	[[ -d "$backup_root" ]] || die "no backup folder at $backup_root"
	if [[ "$require_mount" == 1 ]] && ! mountpoint -q "$backup_root"; then
		die "$backup_root is not a mounted volume; attach the backup block volume first"
	fi

	local stamp dest work
	stamp="$(date -u +%Y%m%dT%H%M%SZ)"
	dest="$backup_root/$stamp"
	work="$backup_root/.partial-$stamp"
	[[ ! -e "$dest" ]] || die "$dest already exists"

	umask 077
	rm -rf "$work"
	mkdir -p "$work"
	trap 'rm -rf "$work"' EXIT

	sqlite3 "$home_dir/hackerblog.db" ".backup '$work/hackerblog.db'"
	local check
	sqlite3 "$work/hackerblog.db" 'PRAGMA journal_mode=DELETE;' >/dev/null
	rm -f "$work/hackerblog.db-wal" "$work/hackerblog.db-shm"
	check="$(sqlite3 "$work/hackerblog.db" 'PRAGMA integrity_check;')"
	[[ "$check" == ok ]] || die "integrity_check of the copy said: $check"

	if [[ -f "$home_dir/sites.toml" ]]; then
		cp "$home_dir/sites.toml" "$work/sites.toml"
	fi
	tar -C "$(dirname "$output_dir")" -czf "$work/output.tar.gz" "$(basename "$output_dir")"
	tar -tzf "$work/output.tar.gz" >/dev/null

	mv "$work" "$dest"
	trap - EXIT

	local old
	while IFS= read -r old; do
		rm -rf "${backup_root:?}/$old"
	done < <(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -exec basename {} \; | sort -r | tail -n +"$((keep + 1))")

	printf 'backup: wrote %s (%s)\n' "$dest" "$(du -sh "$dest" | cut -f1)"
}

main "$@"

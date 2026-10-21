#!/bin/sh
# ############################################################################
# RUN BY A PERSON, AGAINST THE MANAGEMENT CLUSTER. It prompts for credentials.
#
#   sh alertmanager-config.sh
# ############################################################################
#
# Renders alertmanager-config.yaml with its three credentials and installs the
# result as the management cluster's Alertmanager configuration (PET-64): the
# Secret monitoring/alertmanager-radar-alertmanager, key alertmanager.yaml.
#
# Each credential is typed at a prompt that does not echo. From there it goes
# through pipes to amtool and kubectl only -- never into a file, a command's
# argument list or the shell history -- and nothing is changed unless amtool
# accepts the rendered configuration. Re-run it to rotate any credential; it
# asks for all three every time.
#
# Needs: a terminal, and kubectl reaching the management cluster (on cp, its
# default context does).
set -eu

NS=monitoring
SECRET=${SECRET:-alertmanager-radar-alertmanager}
POD=alertmanager-radar-alertmanager-0
TEMPLATE=$(dirname "$0")/alertmanager-config.yaml

[ -r "$TEMPLATE" ] || { echo "cannot read $TEMPLATE" >&2; exit 1; }
# Every placeholder is replaced wherever it appears, comments included, so each
# must appear exactly once: where its value belongs.
for placeholder in __SMTP_PASSWORD__ __HEARTBEAT_URL__ __HEARTBEAT_EMAIL__; do
	[ "$(grep -c "$placeholder" "$TEMPLATE")" -eq 1 ] ||
		{ echo "$TEMPLATE must contain $placeholder exactly once" >&2; exit 1; }
done
[ -t 0 ] || { echo "run this from a terminal: it prompts for credentials" >&2; exit 1; }
kubectl -n "$NS" get pod "$POD" >/dev/null || { echo "cannot reach $NS/$POD" >&2; exit 1; }
# Refuse if the Secret is missing, or if it holds anything besides the
# configuration: this script owns that one key and nothing else.
keys=$(kubectl -n "$NS" get secret "$SECRET" -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}') ||
	{ echo "cannot read $NS/$SECRET" >&2; exit 1; }
[ "$keys" = "alertmanager.yaml " ] ||
	{ echo "$NS/$SECRET holds keys other than alertmanager.yaml ($keys); refusing to drop them" >&2; exit 1; }

# Restore the terminal however the script ends, and let Ctrl-C actually stop it.
trap 'stty echo 2>/dev/null || true' EXIT
trap 'stty echo 2>/dev/null || true; exit 130' INT TERM

# ask NAME PROMPT: read a line into $NAME without echoing it.
ask() {
	printf '%s: ' "$2" >&2
	stty -echo
	IFS= read -r _answer || _answer=
	stty echo
	printf '\n' >&2
	eval "$1=\$_answer"
	_answer=
}

fail() {
	echo "$1; nothing was changed" >&2
	exit 1
}

ask SMTP_PASSWORD "Google app password for yibo@foresightcares.com"
# Google shows it in four groups of four; the spaces are not part of it.
SMTP_PASSWORD=$(printf '%s' "$SMTP_PASSWORD" | tr -d ' ')
case $SMTP_PASSWORD in *[!a-z]* | '') fail "an app password is 16 lowercase letters" ;; esac
[ "${#SMTP_PASSWORD}" -eq 16 ] || fail "an app password is 16 lowercase letters"

ask HEARTBEAT_URL "healthchecks.io ping URL of the \"alertmanager alive\" check"
case $HEARTBEAT_URL in
https://hc-ping.com/?*) ;;
*) fail "the ping URL starts with https://hc-ping.com/" ;;
esac

ask HEARTBEAT_EMAIL "healthchecks.io ping email of the \"alert email path\" check"
case $HEARTBEAT_EMAIL in
?*@hc-ping.com) ;;
*) fail "the ping email ends in @hc-ping.com" ;;
esac

# The values are substituted into single-quoted YAML by awk's gsub, so refuse
# anything that would end the quote or that gsub treats specially.
for value in "$HEARTBEAT_URL" "$HEARTBEAT_EMAIL"; do
	case $value in *[\'\&\\\ ]*) fail "a value contains a quote, &, backslash or space" ;; esac
done

# Exported for awk's ENVIRON, the one place the values leave this shell. For
# the instant awk runs they are readable in its /proc/<pid>/environ, by this
# user and root only; never in an argument list.
export SMTP_PASSWORD HEARTBEAT_URL HEARTBEAT_EMAIL
CONFIG=$(awk '{
	gsub(/__SMTP_PASSWORD__/, ENVIRON["SMTP_PASSWORD"])
	gsub(/__HEARTBEAT_URL__/, ENVIRON["HEARTBEAT_URL"])
	gsub(/__HEARTBEAT_EMAIL__/, ENVIRON["HEARTBEAT_EMAIL"])
	print
}' "$TEMPLATE")
unset SMTP_PASSWORD HEARTBEAT_URL HEARTBEAT_EMAIL

case $CONFIG in *__SMTP_PASSWORD__* | *__HEARTBEAT_URL__* | *__HEARTBEAT_EMAIL__*)
	fail "a placeholder was left unfilled" ;;
esac

printf '%s\n' "$CONFIG" |
	kubectl -n "$NS" exec -i "$POD" -c alertmanager -- amtool check-config /dev/stdin >/dev/null ||
	fail "amtool rejected the rendered configuration"

# Alertmanager's config hash, to tell when it has loaded what is stored next.
config_hash() {
	kubectl -n "$NS" exec "$POD" -c alertmanager -- wget -qO- http://localhost:9093/metrics 2>/dev/null |
		awk '/^alertmanager_config_hash / { print $2 }'
}
before=$(config_hash)
[ -n "$before" ] || fail "cannot read Alertmanager's current config hash, so a reload could not be confirmed"

stored=$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.alertmanager\.yaml}')
wanted=$(printf '%s\n' "$CONFIG" | base64 | tr -d '\n')
if [ "$stored" = "$wanted" ]; then
	stored= wanted= CONFIG=
	echo "$NS/$SECRET already holds exactly this configuration; nothing to do." >&2
	exit 0
fi
stored= wanted=

# Server-side apply: it leaves metadata other tools set on the Secret alone
# (labels and annotations added by kubectl label/annotate or a controller),
# and unlike client-side apply it keeps no second copy of the data,
# credentials included, in a last-applied-configuration annotation. The first
# run takes over what an earlier client-side apply owned, so labels that apply
# set are dropped (the live Secret had none). Then drop the annotation that
# apply left, which holds an old copy of the config.
printf '%s\n' "$CONFIG" |
	kubectl -n "$NS" create secret generic "$SECRET" --from-file=alertmanager.yaml=/dev/stdin \
		--dry-run=client -o yaml |
	kubectl apply --server-side --force-conflicts --field-manager=hydra-alertmanager-config -f - >/dev/null
kubectl -n "$NS" annotate secret "$SECRET" kubectl.kubernetes.io/last-applied-configuration- >/dev/null
CONFIG=
echo "Stored $NS/$SECRET. Waiting for Alertmanager to load it..." >&2

# The operator regenerates its copy and the reloader picks it up; the kubelet's
# Secret sync makes that take up to a couple of minutes.
i=0
while [ "$i" -lt 36 ]; do
	now=$(config_hash)
	if [ -n "$now" ] && [ "$now" != "$before" ]; then
		echo "Alertmanager is running the new configuration." >&2
		exit 0
	fi
	i=$((i + 1))
	sleep 5
done
echo "Alertmanager has not loaded the new configuration after 3 minutes." >&2
echo "Check: kubectl -n $NS logs $POD -c config-reloader" >&2
exit 1

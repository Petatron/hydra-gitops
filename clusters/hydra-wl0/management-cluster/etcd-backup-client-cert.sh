#!/bin/sh
# etcd-backup-client-cert.sh -- mint (or renew) the etcd client certificate the
# hydra-wl0 backup CronJob authenticates with, and store it as a Secret.
#
# APPLIED TO THE MANAGEMENT CLUSTER. Run it with a kubeconfig for the management
# cluster, on a host with openssl -- `cp` has both. Re-running it renews: the
# Secret is replaced, and the next backup run uses the new certificate.
#
# Signed by the workload cluster's etcd CA, which Cluster API keeps in the
# management cluster as Secret <cluster>-etcd. The CA key is read into a 0700
# directory, on tmpfs where the host has one, only for the signing, and the
# directory is removed on exit. That is all: shred is unreliable on journaled
# and SSD filesystems, so do not count on more. Nothing here is printed, and no
# key ever reaches git.
#
# What this certificate can do, plainly: etcd as kubeadm and Cluster API deploy
# it checks the client certificate and nothing else -- there is no etcd RBAC --
# so this certificate can read AND WRITE all of hydra-wl0's etcd, bypassing its
# API server. The backup only reads, but etcd cannot express that. Anyone who
# can get Secret hydra-backup/etcd-backup-<cluster>-client holds that power.
# That adds no one: every subject that can read Secrets there (cluster-admins,
# the Cluster API controllers, and others with cluster-wide Secret read,
# checked 2026-10-17) can already read the etcd CA itself, which can mint any
# such certificate. The backup's own ServiceAccount cannot read it; the pod gets
# it as a volume.
#
# The certificate is valid for 365 days. A daily check, CronJob
# etcd-client-cert-check-<cluster>, starts failing 30 days before expiry, and
# EtcdBackupClientCertExpiring says it is time to run this again.
set -eu

CLUSTER=${1:-hydra-wl0}
CLUSTER_NAMESPACE=${CLUSTER_NAMESPACE:-default}
BACKUP_NAMESPACE=${BACKUP_NAMESPACE:-hydra-backup}
DAYS=${DAYS:-365}
SECRET=etcd-backup-$CLUSTER-client

# tmpfs where there is one, so the CA key never reaches a disk; plain mktemp
# otherwise.
d=""
for base in ${XDG_RUNTIME_DIR:-} /dev/shm; do
	[ -n "$base" ] && [ -d "$base" ] && [ -w "$base" ] && d=$(mktemp -d "$base/etcd-backup-cert.XXXXXX" 2>/dev/null) && break
done
[ -n "$d" ] || d=$(mktemp -d)
chmod 700 "$d"
# INT and TERM exit explicitly: in sh a trap runs and the script carries on.
trap 'shred -u "$d"/* 2>/dev/null || true; rm -rf "$d"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
umask 077

kubectl -n "$CLUSTER_NAMESPACE" get secret "$CLUSTER-etcd" -o jsonpath='{.data.tls\.crt}' | base64 -d >"$d/ca.crt"
kubectl -n "$CLUSTER_NAMESPACE" get secret "$CLUSTER-etcd" -o jsonpath='{.data.tls\.key}' | base64 -d >"$d/ca.key"
[ -s "$d/ca.crt" ] && [ -s "$d/ca.key" ] || { echo "Secret $CLUSTER_NAMESPACE/$CLUSTER-etcd has no CA" >&2; exit 1; }

openssl ecparam -genkey -name prime256v1 -noout -out "$d/tls.key"
openssl req -new -key "$d/tls.key" -subj "/CN=hydra-etcd-backup" -out "$d/tls.csr"
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n' >"$d/ext"
# Quiet on success, but say why on failure: under set -e a silent failure here
# would exit with no message at all.
if ! openssl x509 -req -in "$d/tls.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -set_serial "0x$(openssl rand -hex 16)" \
	-days "$DAYS" -extfile "$d/ext" -out "$d/tls.crt" 2>"$d/sign.err"; then
	echo "signing the client certificate failed:" >&2
	cat "$d/sign.err" >&2
	exit 1
fi
openssl verify -CAfile "$d/ca.crt" "$d/tls.crt" >/dev/null

# Server-side apply. A client-side `kubectl apply` would copy the whole Secret,
# private key included, into the last-applied-configuration annotation, which
# `kubectl describe` prints.
kubectl -n "$BACKUP_NAMESPACE" create secret generic "$SECRET" \
	--from-file=ca.crt="$d/ca.crt" --from-file=tls.crt="$d/tls.crt" --from-file=tls.key="$d/tls.key" \
	--dry-run=client -o yaml | kubectl apply --server-side --force-conflicts --field-manager=etcd-backup-client-cert -f - >/dev/null
echo "Stored $BACKUP_NAMESPACE/$SECRET, valid until $(openssl x509 -noout -enddate -in "$d/tls.crt" | cut -d= -f2)"

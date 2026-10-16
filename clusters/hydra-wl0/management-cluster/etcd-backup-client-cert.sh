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
# temporary directory only for the signing, and shredded on exit with the rest.
# Nothing here is printed, and no key ever reaches git.
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

d=$(mktemp -d)
chmod 700 "$d"
# INT and TERM exit explicitly: in sh a trap runs and the script carries on.
trap 'shred -u "$d"/* 2>/dev/null; rm -rf "$d"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
umask 077

kubectl -n "$CLUSTER_NAMESPACE" get secret "$CLUSTER-etcd" -o jsonpath='{.data.tls\.crt}' | base64 -d >"$d/ca.crt"
kubectl -n "$CLUSTER_NAMESPACE" get secret "$CLUSTER-etcd" -o jsonpath='{.data.tls\.key}' | base64 -d >"$d/ca.key"
[ -s "$d/ca.crt" ] && [ -s "$d/ca.key" ] || { echo "Secret $CLUSTER_NAMESPACE/$CLUSTER-etcd has no CA" >&2; exit 1; }

openssl ecparam -genkey -name prime256v1 -noout -out "$d/tls.key"
openssl req -new -key "$d/tls.key" -subj "/CN=hydra-etcd-backup" -out "$d/tls.csr"
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n' >"$d/ext"
openssl x509 -req -in "$d/tls.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -set_serial "0x$(openssl rand -hex 16)" \
	-days "$DAYS" -extfile "$d/ext" -out "$d/tls.crt" 2>/dev/null
openssl verify -CAfile "$d/ca.crt" "$d/tls.crt" >/dev/null

# Server-side apply. A client-side `kubectl apply` would copy the whole Secret,
# private key included, into the last-applied-configuration annotation, which
# `kubectl describe` prints.
kubectl -n "$BACKUP_NAMESPACE" create secret generic "$SECRET" \
	--from-file=ca.crt="$d/ca.crt" --from-file=tls.crt="$d/tls.crt" --from-file=tls.key="$d/tls.key" \
	--dry-run=client -o yaml | kubectl apply --server-side --force-conflicts --field-manager=etcd-backup-client-cert -f - >/dev/null
echo "Stored $BACKUP_NAMESPACE/$SECRET, valid until $(openssl x509 -noout -enddate -in "$d/tls.crt" | cut -d= -f2)"

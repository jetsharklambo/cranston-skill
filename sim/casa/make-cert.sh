#!/bin/bash
# make-cert.sh <name> <days> - (re)generate a self-signed PEM at
# $CASA_RUN/certs/<name>.pem valid for <days> from the REAL clock.
#
# Why real clock: checks/templates/check-cert-expiry.sh computes days-left
# against datetime.now() in a child process the soak's patched sim clock
# cannot reach, so scenario event cert_set regenerates the PEM instead of
# moving time (plan conflict #3). Consequence: openssl stamps notAfter from
# the wall clock at generation, so the human-readable date inside the
# CERT_EXPIRING alert text is the one non-sim-deterministic substring in a
# soak's output; sim/soak.py --selftest masks exactly that (see sim/README.md).
set -eu
NAME="${1:?usage: make-cert.sh <name> <days>}"
DAYS="${2:?usage: make-cert.sh <name> <days>}"
RUN="${CASA_RUN:?CASA_RUN not set}"
mkdir -p "$RUN/certs"
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$RUN/certs/$NAME.key" -out "$RUN/certs/$NAME.pem" \
    -days "$DAYS" -subj "/CN=$NAME.casa.sim" >/dev/null 2>&1

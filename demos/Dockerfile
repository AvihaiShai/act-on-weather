# The proof runner: a container that can execute demos/*.sh on any OS.
#
# Why this exists. Every demo script drives the stack through `docker compose`
# and reads the API with curl and python3. On Linux and macOS those are host
# tools; on Windows none of them are, so the platform the README documents in
# the most detail could reach none of the proofs. Putting the shell in a
# container makes one set of commands work everywhere, with Docker as the only
# host dependency.
#
# The scripts themselves are NOT copied in -- they are bind-mounted from the
# working tree at /work. A proof that runs a stale copy of itself is worthless,
# and this way `demos/*.sh` stays the single implementation for both this
# container and `bash demos/01_offline.sh` on a host that has bash.
FROM docker:28-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d

# bash for the scripts' arrays and `set -o pipefail`, python3 for the JSON
# parsing in demos/lib.sh, curl for the API calls. Versions are not pinned:
# the base image is pinned by digest, but Alpine's package index moves under
# it, and a build that breaks whenever upstream publishes a patch is worse than
# an unpinned tooling image that ships nothing into the runtime stack.
RUN apk add --no-cache bash curl python3

WORKDIR /work
ENTRYPOINT ["bash", "/work/demos/run.sh"]
CMD ["help"]

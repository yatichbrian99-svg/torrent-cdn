# MediaMTX + busybox applets — the distroless upstream image has no shell, so
# runOnReady/runOnNotReady hook commands (executed as `/bin/sh -c …`) cannot
# run. We copy busybox's whole /bin (sh, wget, and every applet symlink) into
# the image: ~1 MB, static musl binary, no libc dependency. The entrypoint,
# the image user, and the MediaMTX binary are untouched.
#
# Distroless constraints that rule out the "obvious" approaches:
#   - no /bin/sh      → `RUN sh -c …` cannot work (use multi-stage COPY)
#   - no /etc/passwd  → `USER root` cannot resolve (numeric user inherited)
FROM busybox:1.36.1-musl AS busybox

FROM bluenviron/mediamtx:1.11.3

COPY --from=busybox /bin /bin

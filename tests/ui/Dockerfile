# Headless-browser gate (F11), CI-only -- this image is never part of the
# running stack, never referenced by scripts/package-offline.sh, and its
# digest in IMAGES.lock is marked CI-only for the same reason. Browsers are
# baked into the base image at /ms-playwright -- `pip install playwright`
# below pulls only the small Python package, matched to the image's own
# version, so it finds the browsers already there instead of downloading
# them. Verified locally: `docker run --rm --network none <this image> ...`
# launches Chromium fine, i.e. nothing here downloads a browser at run time.
#
# The `pip install` below DOES need network access at *build* time (CI has
# it -- see the top of ci.yml). That is a build-time cost of a CI-only image,
# not a runtime one, and it never touches the air-gapped runtime bundle.
FROM mcr.microsoft.com/playwright/python@sha256:acc6300abda1a16dba3a0d39583c8be53e619a3f23d4a0eeeb8de0a9275c47ac

WORKDIR /work
RUN pip install --no-cache-dir playwright==1.48.0 requests==2.32.3

COPY tests/__init__.py tests/__init__.py
COPY tests/ui/ tests/ui/

RUN useradd -u 10001 -m appuser && chown -R appuser /work
USER 10001

ENTRYPOINT ["python3", "-m", "tests.ui.browser_gate"]

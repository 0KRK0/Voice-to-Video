# Dependency locking — current state and the gap

**STATUS: PARTIAL.** Versions are pinned. Artefact hashes are not.

## What exists

`requirements.txt` pins every runtime dependency to an exact version, and those
versions are the ones the test suite actually ran against. The Dockerfile
installs from it, so the image contains what was tested rather than whatever
resolved on build day.

## What does not exist, and why

`pip install --require-hashes` is the control that matters. Version pinning
defends against an accidental upgrade; hash pinning defends against a
substituted artefact — a compromised index, a typosquat resolving first, a
mirror serving a modified wheel. They are different threats and only the second
one is an attack.

Generating that file requires downloading every wheel from a package index and
recording its digest. **This build environment has no access to a package
index** (`pip download` and `uv` both fail with 403 from `pypi.org`), so any
hash written here would be a number I made up, and `--require-hashes` against
invented digests fails closed at build time — which is a worse outcome than not
claiming the control at all.

This is an EXTERNAL DEPENDENCY, not a design decision.

## How to close it

In any environment with index access:

```bash
pip install pip-tools
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt
```

Then change the Dockerfile's install step to:

```dockerfile
COPY requirements.lock ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes -r requirements.lock
```

`tests/test_deployment.py::TheDockerfileMatchesTheRepository` asserts that the
Dockerfile installs from a requirements file that exists in the repository, so
this swap cannot be made halfway.

## Rebuild reproducibility beyond hashes

Two further gaps, stated so they are not mistaken for done:

* The base image is `python:3.11-slim-bookworm` by tag, not by digest. A tag
  moves. Pinning `python:3.11-slim-bookworm@sha256:…` requires a registry
  lookup, which is the same blocked network.
* `apt-get install` resolves ffmpeg and the Noto fonts at build time against
  Debian's current archive, so two builds a month apart can differ. Debian
  snapshot archives fix this; adopting one is a decision about how much build
  reproducibility is worth to this deployment, not an oversight.

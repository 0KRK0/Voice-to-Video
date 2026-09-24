"""P0-11 — the deployment artifacts describe *this* repository.

The audit scored deployment 8/100, and the reason was not that a Dockerfile was
missing. It was that nothing connected the deployment story to the code: a
compose file could name an entrypoint that did not exist, install from a lock
file that was never generated, or start a PostgreSQL service the application
never contacts, and every test would still pass.

So these tests read the artifacts as data and assert they agree with the code.
They are cheap and slightly unusual, and they are the only thing standing
between "the container starts" and "the container starts on someone else's
Tuesday".

They deliberately do NOT run Docker. Docker is not available here, and a test
that silently skips is worse than one that checks something real: every
assertion below is a genuine consistency check, and the things that genuinely
need a container to prove are named in `deploy/README.md` as unproven.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.config import Settings, read_env_file

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"


def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _instructions(body: str) -> str:
    """The file with its commentary removed.

    Both artifacts explain at length what they deliberately do *not* do, and
    those explanations name the very strings some assertions forbid. Checking
    the directives rather than the prose is the difference between a test that
    verifies behaviour and one that bans a word.
    """
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


class TheArtifactsExist(unittest.TestCase):
    def test_every_referenced_file_is_in_the_repository(self) -> None:
        for name in ("Dockerfile", ".dockerignore", "deploy/docker-compose.yml",
                     "deploy/README.md", "migrations/README.md", "requirements.txt"):
            with self.subTest(name):
                self.assertTrue((ROOT / name).exists(), f"{name} is referenced but absent")


class TheDockerfileMatchesTheRepository(unittest.TestCase):
    def test_it_installs_from_a_requirements_file_that_exists(self) -> None:
        """The defect this test exists for.

        The first draft installed from `requirements.lock` with
        `--require-hashes`. That file was never generated, because this build
        environment cannot reach a package index — so the image could not have
        built, and nothing said so.
        """
        referenced = set(re.findall(r"-r\s+(\S+)", _instructions(dockerfile())))
        self.assertTrue(referenced, "the Dockerfile installs no requirements file")
        for name in referenced:
            with self.subTest(name):
                self.assertTrue(
                    (ROOT / name).exists(),
                    f"the Dockerfile installs from {name}, which does not exist",
                )

    def test_it_does_not_claim_hash_verification_it_cannot_perform(self) -> None:
        """`--require-hashes` against invented digests fails closed at build."""
        if "--require-hashes" in _instructions(dockerfile()):
            self.assertTrue(
                (ROOT / "requirements.lock").exists(),
                "the Dockerfile requires hashes but no hashed lock file exists",
            )

    def test_the_pinned_versions_are_exact(self) -> None:
        """A range means the container runs code no test has seen."""
        body = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for line in body.splitlines():
            entry = line.split("#", 1)[0].strip()
            if not entry:
                continue
            with self.subTest(entry):
                self.assertIn("==", entry, f"{entry!r} is not pinned to one version")
                self.assertNotIn(">", entry)
                self.assertNotIn("<", entry)

    def test_every_runtime_import_is_pinned(self) -> None:
        """The image must contain what the code imports.

        Catches the failure where a new adapter's dependency is added to
        `pyproject.toml`'s extras and forgotten here, so the container builds
        and then raises ImportError on the first request that needs it.
        """
        pinned = {
            line.split("==")[0].strip().lower().replace("-", "_")
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if "==" in line and not line.strip().startswith("#")
        }
        for module in ("pydantic", "starlette", "uvicorn", "pillow", "numpy", "httpx"):
            with self.subTest(module):
                self.assertIn(module, pinned)

    def test_it_runs_as_a_non_root_user(self) -> None:
        """A parser exploit in a malicious document must not land on root."""
        body = dockerfile()
        self.assertTrue(
            re.search(r"^USER\s+(?!root\b)\S+", body, re.MULTILINE),
            "the image runs as root",
        )
        self.assertLess(
            body.rindex("COPY --from=build"),
            body.rindex("\nUSER "),
            "USER must come after the last privileged step",
        )

    def test_it_installs_ffmpeg_because_the_renderer_needs_it(self) -> None:
        self.assertIn("ffmpeg", _instructions(dockerfile()))

    def test_the_healthcheck_targets_an_endpoint_the_api_serves(self) -> None:
        probes = set(re.findall(r"/health/\w+", _instructions(dockerfile())))
        self.assertTrue(probes)
        for path in probes:
            with self.subTest(path):
                self.assertIn(path, routes_declared())


class TheEntrypointsResolve(unittest.TestCase):
    def test_the_dockerfile_cmd_names_a_real_asgi_object(self) -> None:
        match = re.search(
            r'"uvicorn"\s*,\s*"([\w.]+):(\w+)"', _instructions(dockerfile())
        )
        self.assertIsNotNone(match, "the Dockerfile CMD does not name uvicorn a target")
        assert match is not None
        module_name, attribute = match.group(1), match.group(2)

        import importlib

        module = importlib.import_module(module_name)
        self.assertTrue(
            hasattr(module, attribute),
            f"{module_name}:{attribute} is the container's entrypoint and does "
            "not exist",
        )
        self.assertTrue(callable(getattr(module, attribute)))

    def test_the_compose_api_command_names_the_same_object(self) -> None:
        self.assertIn("vtv.api.app:application", _instructions(compose()))
        self.assertIn("vtv.api.app:application", _instructions(dockerfile()))

    def test_the_asgi_object_is_lazy(self) -> None:
        """Importing the module must not build an assembly.

        If it did, `python -m vtv.migrate` and `python -m vtv.worker` — which
        import from the same package — would each open an API's worth of state,
        and a wiring failure would surface as "cannot import" with no server
        left to answer the probe that explains it.
        """
        from vtv.api.app import application

        self.assertIsNone(application._app)

    def test_the_worker_module_is_runnable(self) -> None:
        match = re.search(
            r'"python",\s*"-m",\s*"([\w.]+)"', _instructions(compose())
        )
        self.assertIsNotNone(match)
        assert match is not None

        import importlib

        module = importlib.import_module(match.group(1))
        self.assertTrue(hasattr(module, "main"))

    def test_the_migrate_module_is_runnable(self) -> None:
        self.assertIn("vtv.migrate", _instructions(compose()))
        from vtv import migrate

        self.assertTrue(callable(migrate.main))


class TheComposeTopologyIsHonest(unittest.TestCase):
    def test_it_declares_more_than_one_replica_of_each_role(self) -> None:
        """The whole P0-4/5/6 series exists so this is true."""
        self.assertGreaterEqual(_instructions(compose()).count("replicas: 2"), 2)

    def test_migrations_complete_before_anything_serves(self) -> None:
        self.assertIn(
            "service_completed_successfully", _instructions(compose())
        )

    def test_it_starts_no_service_the_application_never_contacts(self) -> None:
        """The defect this test exists for.

        An earlier draft started PostgreSQL and Redis and set
        `VTV_DATABASE_URL: postgresql://…`. The code implements neither, and
        `repository_path()` silently fell back to a local SQLite file — so each
        replica wrote to its own private database while the compose file looked
        like a distributed deployment.
        """
        body = _instructions(compose())
        service_block = body.split("\nservices:", 1)[1]
        for absent in ("image: postgres", "image: redis"):
            with self.subTest(absent):
                self.assertNotIn(absent, service_block)
        self.assertNotIn("postgresql://", body)
        self.assertNotIn("redis://", body)

    def test_the_database_url_uses_a_scheme_the_code_implements(self) -> None:
        match = re.search(r"VTV_DATABASE_URL:\s*(\S+)", _instructions(compose()))
        self.assertIsNotNone(match)
        assert match is not None
        self.assertTrue(match.group(1).startswith("sqlite:"))

    def test_state_is_on_one_shared_volume(self) -> None:
        """Two replicas mean nothing if each has its own queue file."""
        body = _instructions(compose())
        self.assertIn("state:/var/lib/vtv", body)
        storage = re.search(r"VTV_STORAGE_ROOT:\s*(\S+)", body)
        database = re.search(r"VTV_DATABASE_URL:\s*sqlite:/*(\S+)", body)
        assert storage is not None and database is not None
        self.assertTrue(storage.group(1).startswith("/var/lib/vtv"))
        self.assertTrue(("/" + database.group(1)).startswith("/var/lib/vtv"))

    def test_the_signing_key_is_required_rather_than_defaulted(self) -> None:
        """A per-replica key breaks downloads behind a load balancer."""
        self.assertRegex(
            _instructions(compose()), r"VTV_SIGNING_KEY:\s*\$\{VTV_SIGNING_KEY:\?"
        )

    def test_the_stop_grace_exceeds_the_workers_own_drain_window(self) -> None:
        from vtv.worker import SHUTDOWN_GRACE_SECONDS

        match = re.search(r"stop_grace_period:\s*(\d+)s", _instructions(compose()))
        self.assertIsNotNone(match)
        assert match is not None
        self.assertGreater(
            int(match.group(1)),
            SHUTDOWN_GRACE_SECONDS,
            "the orchestrator would kill a worker that was still draining",
        )

    def test_the_api_readiness_probe_targets_readiness_not_liveness(self) -> None:
        self.assertIn("/health/ready", _instructions(compose()))


class ConfigurationHasNoDeadEntries(unittest.TestCase):
    def test_every_setting_can_be_set_from_the_environment(self) -> None:
        """A declared setting nobody can configure is a trap.

        It reads as supported, so a deployment sets `VTV_…`, nothing happens,
        and the default silently governs production.
        """
        from vtv.config import Settings

        source = {
            "VTV_ENV": "production",
            "VTV_SIGNING_KEY": "k" * 32,
            "VTV_SIGNED_URL_SECONDS": "60",
            "VTV_MAX_UPLOAD_BYTES": "1024",
        }
        settings = Settings.from_env(source)
        self.assertEqual(settings.signing_key, "k" * 32)
        self.assertEqual(settings.signed_url_seconds, 60)
        self.assertEqual(settings.max_upload_bytes, 1024)

    def test_the_example_file_names_only_variables_the_code_reads(self) -> None:
        """`.env.example` is documentation that can be wrong.

        It listed `VTV_REDIS_URL`, `VTV_STORAGE_ACCESS_KEY`,
        `VTV_STORAGE_SECRET_KEY` and `VTV_ASSET_SEARCH_API_KEY` — four variables
        the code has never read. An operator setting one of those gets silence
        and the default.
        """
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        declared = {
            line.split("=", 1)[0].strip()
            for line in example.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
        }
        self.assertTrue(declared)
        unknown = sorted(declared - accepted_environment_keys())
        self.assertEqual(unknown, [], f"{unknown} is documented but never read")

    def test_every_variable_the_compose_file_sets_is_read(self) -> None:
        body = _instructions(compose())
        declared = set(re.findall(r"^\s*(VTV_[A-Z_]+):", body, re.MULTILINE))
        unknown = sorted(declared - accepted_environment_keys())
        self.assertEqual(unknown, [], f"compose sets {unknown}, which nothing reads")

    def test_the_removed_redis_setting_stays_removed(self) -> None:
        """It was configuration for a dependency nothing used.

        Dead configuration is what let the compose file declare Redis with a
        straight face. Removing it is the fix; this keeps it removed.
        """
        from vtv.config import Settings

        self.assertNotIn("redis_url", Settings.model_fields)

    def test_the_signing_key_is_never_logged(self) -> None:
        from vtv.config import Settings

        settings = Settings(asset_search_endpoint="", signing_key="super-secret-value")
        self.assertNotIn("super-secret-value", str(settings.redacted()))


class ProductionFailsClosed(unittest.TestCase):
    def test_production_without_a_signing_key_refuses_to_start(self) -> None:
        from vtv.config import Settings
        from vtv.contracts.errors import VTVError
        from vtv.wiring import build

        with self.assertRaises(VTVError):
            build(Settings(asset_search_endpoint="", env="production", signing_key=None))

    def test_an_unimplemented_database_scheme_refuses_to_start(self) -> None:
        """Better than quietly writing to a file nobody configured."""
        from vtv.config import Settings
        from vtv.contracts.errors import VTVError
        from vtv.wiring import repository_path

        with self.assertRaises(VTVError):
            repository_path(Settings(asset_search_endpoint="", database_url="postgresql://vtv@db/vtv"))


def accepted_environment_keys() -> set[str]:
    """Every `VTV_…` name `Settings.from_env` actually consults.

    Read from the source of `config.py` rather than by probing, because the
    three readers (`get`, `path`, `number`) all take a bare suffix and there is
    no runtime registry to ask.
    """
    body = (ROOT / "src" / "vtv" / "config.py").read_text(encoding="utf-8")
    names = re.findall(r'\b(?:get|path|number)\(\s*"([A-Z_]+)"', body)
    return {f"VTV_{name}" for name in names}


def routes_declared() -> set[str]:
    """Every path the API serves, read from the source.

    Reading the source rather than building an app keeps this test free of a
    database, which matters because it runs in environments where one is not
    guaranteed.
    """
    body = (ROOT / "src" / "vtv" / "api" / "app.py").read_text(encoding="utf-8")
    return set(re.findall(r'Route\(\s*"([^"]+)"', body))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheDotEnvFileIsActuallyRead(unittest.TestCase):
    """The defect: `.env` was documentation shaped like configuration.

    `.env.example` opens with "Copy to `.env` for local development". Every
    runbook says to do it. `deploy/docker-compose.yml` passes one. And nothing
    in this codebase had ever read one — `Settings.from_env` read `os.environ`
    and only `os.environ`.

    The failure it produced was silent and expensive: an operator sets
    `VTV_TEXT_GENERATION_API_KEY` in `.env`, restarts, and gets a system that
    reports the capability as unconfigured, with the file sitting right there
    looking correct. Docker never exposed it, because `--env-file` is read by
    Docker rather than by us — which is exactly why it survived a production
    deployment validation.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-dotenv-")
        self.root = Path(self._dir.name)
        self.path = self.root / ".env"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_credential_in_the_file_reaches_settings(self) -> None:
        self.path.write_text(
            "VTV_TEXT_GENERATION_ENDPOINT=https://api.openai.com/v1\n"
            "VTV_TEXT_GENERATION_API_KEY=sk-from-the-file\n",
            encoding="utf-8",
        )
        values = read_env_file(self.path)
        self.assertEqual(values["VTV_TEXT_GENERATION_API_KEY"], "sk-from-the-file")
        settings = Settings.from_env({**values})
        self.assertEqual(settings.text_generation_api_key, "sk-from-the-file")
        self.assertEqual(
            settings.text_generation_endpoint, "https://api.openai.com/v1"
        )

    def test_the_real_environment_beats_the_file(self) -> None:
        """The only safe precedence.

        A secrets manager, a shell export or `--env-file` must beat a stale
        line in a checked-out file. The reverse would let a committed default
        silently override an injected production secret.
        """
        self.path.write_text("VTV_LOG_LEVEL=debug\n", encoding="utf-8")
        merged = {**read_env_file(self.path), "VTV_LOG_LEVEL": "warning"}
        self.assertEqual(Settings.from_env(merged).log_level, "warning")

    def test_quotes_comments_exports_and_junk_are_handled(self) -> None:
        self.path.write_text(
            "# a comment\n"
            "\n"
            'VTV_SPEECH_SYNTHESIS_VOICE="nova"\n'
            "export VTV_STORAGE_BUCKET='my-bucket'\n"
            "a line with no equals sign at all\n"
            "VTV_LOG_LEVEL=  debug  \n",
            encoding="utf-8",
        )
        values = read_env_file(self.path)
        self.assertEqual(values["VTV_SPEECH_SYNTHESIS_VOICE"], "nova")
        self.assertEqual(values["VTV_STORAGE_BUCKET"], "my-bucket")
        self.assertEqual(values["VTV_LOG_LEVEL"], "debug")
        self.assertNotIn("a line with no equals sign at all", values)

    def test_a_missing_file_is_not_an_error(self) -> None:
        """The normal case in a container, and never a reason to refuse to boot."""
        self.assertEqual(read_env_file(self.root / "nope.env"), {})

    def test_every_variable_the_example_declares_is_parsed(self) -> None:
        """The example file must survive its own parser.

        The sibling test in this module proves every documented variable is
        read by `Settings`. This proves the file can be read at all — the two
        together are what make `.env` configuration rather than decoration.
        """
        values = read_env_file(ROOT / ".env.example")
        self.assertTrue(values)
        unknown = sorted(set(values) - accepted_environment_keys())
        self.assertEqual(unknown, [], f"{unknown} is documented but never read")

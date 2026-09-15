from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts/install-meshcentral-workstation.sh"
AGENT = ROOT / "scripts/install-meshagent-intel-edge.sh"


class MeshCentralInstallerTests(unittest.TestCase):
    def test_shell_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(SERVER), str(AGENT)], check=True)

    def test_help_documents_required_operator_inputs(self) -> None:
        server = subprocess.run(
            ["bash", str(SERVER), "--help"], capture_output=True, text=True, check=True
        ).stderr
        self.assertIn("--server-name", server)
        self.assertIn("--meshcentral-version", server)
        self.assertIn("--verify-only", server)

        agent = subprocess.run(
            ["bash", str(AGENT), "--help"], capture_output=True, text=True, check=True
        ).stderr
        self.assertIn("--mesh-id-file", agent)
        self.assertIn("--ca-cert", agent)
        self.assertIn("--reinstall", agent)
        self.assertIn("--agent-sha256", agent)

    def test_server_is_pinned_and_runs_as_a_restricted_account(self) -> None:
        script = SERVER.read_text(encoding="utf-8")
        for expected in (
            'DEFAULT_MESHCENTRAL_VERSION="1.2.5"',
            'DEFAULT_NODE_VERSION="24.21.0"',
            'User=${SERVICE_USER}',
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            '"selfUpdate": False',
            '"exactPorts": True',
            '"newAccounts": False',
            "--save-exact",
            "SHASUMS256.txt",
            'chown -R "root:${SERVICE_GROUP}" "${release}"',
            'chmod -R g+rX,o-rwx,go-w "${release}"',
            '"https://127.0.0.1:${HTTPS_PORT}/health.ashx"',
            '--header "Host: ${SERVER_NAME}:${HTTPS_PORT}"',
            "--noproxy '*'",
            '[[ ${response} == "ok" ]]',
        ):
            self.assertIn(expected, script)
        self.assertNotIn("npm install -g", script)

    def test_agent_has_no_insecure_or_http_download_path(self) -> None:
        script = AGENT.read_text(encoding="utf-8")
        download = script.split("download_agent_inputs()", 1)[1].split(
            "copy_offline_inputs()", 1
        )[0]
        self.assertIn("--proto '=https'", download)
        self.assertIn("--tlsv1.2", download)
        self.assertIn("--cacert", download)
        self.assertNotIn("--insecure", download)
        self.assertNotIn("http://", download)
        self.assertNotIn("meshagents?script=1", script)
        self.assertIn("'id=6'", download)

    def test_agent_cleanup_does_not_reference_a_function_local_at_exit(self) -> None:
        script = AGENT.read_text(encoding="utf-8")
        self.assertIn('TEMPORARY_DIRECTORY=""', script)
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn("${TEMPORARY_DIRECTORY:-}", script)
        self.assertNotIn("trap 'rm -rf", script)

    def test_agent_url_normalization_rejects_unsafe_urls(self) -> None:
        source = f"source {AGENT}; normalize_server_url"
        accepted = subprocess.run(
            ["bash", "-c", f"{source} https://mesh.example:8443"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(accepted.stdout.strip(), "https://mesh.example:8443")
        for unsafe in (
            "http://mesh.example",
            "https://user:password@mesh.example",
            "https://mesh.example/path",
            "https://mesh.example/?key=value",
        ):
            result = subprocess.run(
                ["bash", "-c", f"{source} '{unsafe}'"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0, unsafe)

    def test_agent_artifact_validation_checks_architecture_and_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            agent = temporary / "meshagent"
            header = bytearray(20)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = (62).to_bytes(2, "little")
            agent.write_bytes(bytes(header) + b"\0" * 100_000)
            settings = temporary / "meshagent.msh"
            settings.write_text(
                "MeshName=TVT Intel Edges\n"
                "MeshType=2\n"
                f"MeshID=0x{'a' * 96}\n"
                f"ServerID={'b' * 96}\n"
                "MeshServer=wss://mesh.example:443/agent.ashx\n",
                encoding="utf-8",
            )
            command = (
                f"source {AGENT}; SERVER_URL=https://mesh.example; "
                f"validate_agent_binary {agent}; validate_settings {settings}"
            )
            subprocess.run(["bash", "-c", command], check=True)
            mismatch = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {AGENT}; SERVER_URL=https://other.example; "
                    f"validate_settings {settings}",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(mismatch.returncode, 0)

    def test_documentation_covers_identity_backup_and_both_agent_modes(self) -> None:
        documentation = (ROOT / "docs/MESHCENTRAL-NATIVE-INSTALL.md").read_text(
            encoding="utf-8"
        )
        for expected in (
            "server's agent identity keys",
            "Install the agent online",
            "Install the agent offline",
            "--mesh-id-file",
            "--verify-only",
            "no inbound firewall",
        ):
            self.assertIn(expected, documentation)


if __name__ == "__main__":
    unittest.main()

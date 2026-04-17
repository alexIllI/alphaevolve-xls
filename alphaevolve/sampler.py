"""
alphaevolve/sampler.py
──────────────────────
Interfaces with the AI backend to generate C++ scheduling algorithm implementations.

Backends:
  1. codex  — `codex exec` (v0.120+) non-interactive mode, uses ChatGPT
               subscription auth set up when the user first ran `codex` interactively.
               Prompt piped via stdin, model response written to file via -o flag.
  2. openai — Python SDK direct API call with model fallback chain.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


PROMPTS_DIR = Path(__file__).parent / "prompts"
KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"


class Sampler:
    """Generates C++ scheduling algorithm implementations via AI."""

    def __init__(
        self,
        backend: str = "codex",
        model: str = "gpt-5.4",        # default = what codex TUI showed
        api_key: str | None = None,
        max_tokens: int = 8192,
    ):
        self.backend = backend
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.max_tokens = max_tokens

        self._jinja = Environment(
            loader=FileSystemLoader(str(PROMPTS_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._system_prompt = (PROMPTS_DIR / "system.txt").read_text()

    # ── Public API ─────────────────────────────────────────────────────────────

    def sample(
        self,
        mutation_target: str,
        mutation_instruction: str,
        current_source: str,
        reference_source_bundle: str,
        best_score: float,
        best_num_stages: int,
        best_reg_bits: int,
        best_delay_ps: int,
        parent_score: float,
        parent_num_stages: int,
        parent_reg_bits: int,
        parent_delay_ps: int,
        knowledge_keys: list[str] | None = None,
        compile_error: str | None = None,   # set on retry so AI can fix its mistake
        target_file_path: str = "",
    ) -> str:
        """Generate a new C++ implementation. Returns the raw C++ string."""
        knowledge_context = self._load_knowledge(knowledge_keys or [])

        template_name = (
            "implement_agent_scheduler.txt"
            if mutation_target == "agent_scheduler"
            else "implement.txt"
        )
        template = self._jinja.get_template(template_name)
        user_prompt = template.render(
            mutation_target=mutation_target,
            mutation_instruction=mutation_instruction,
            current_function_source=current_source,
            current_source=current_source,
            sdc_scheduler_source=reference_source_bundle,
            reference_source_bundle=reference_source_bundle,
            best_score=best_score,
            best_num_stages=best_num_stages,
            best_reg_bits=best_reg_bits,
            best_delay_ps=best_delay_ps,
            parent_score=parent_score,
            parent_num_stages=parent_num_stages,
            parent_reg_bits=parent_reg_bits,
            parent_delay_ps=parent_delay_ps,
            knowledge_context=knowledge_context,
            compile_error=compile_error,    # None on first attempt
            target_file_path=target_file_path,
        )

        if self.backend == "codex":
            raw = self._call_codex(user_prompt)
        else:
            raw = self._call_openai(user_prompt)

        return self._extract_cpp(raw)


    # ── Codex CLI backend (primary) ────────────────────────────────────────────

    def _call_codex(self, user_prompt: str) -> str:
        """
        Call codex v0.120+ non-interactively via `codex exec`:

          codex exec
            -m <model>
            --sandbox read-only       ← pure text generation, no shell execution
            --skip-git-repo-check     ← allow running in /tmp
            --ephemeral               ← don't persist session files
            -o <output_file>          ← writes model's final response to this file
            -                         ← read prompt from stdin

        The -o flag is the key: it captures the model's last message text directly
        to a file, so we get clean C++ output without any extra parsing complexity.
        """
        pid = os.getpid()
        output_file = Path(f"/tmp/alphaevolve_output_{pid}.cpp")

        try:
            if output_file.exists():
                output_file.unlink()

            full_prompt = (
                f"{self._system_prompt}\n\n"
                f"{user_prompt}\n\n"
                "CRITICAL: Your ENTIRE response must be valid C++ source code only. "
                "No prose, no explanations, no markdown fences. Pure C++ only."
            )

            env = {**os.environ}
            if self.api_key:
                env["OPENAI_API_KEY"] = self.api_key

            result = subprocess.run(
                [
                    "codex", "exec",
                    "-m", self.model,
                    "--sandbox", "read-only",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "-o", str(output_file),
                    "-",                      # prompt from stdin
                ],
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )

            if output_file.exists() and output_file.stat().st_size > 0:
                return output_file.read_text(encoding="utf-8")

            combined = (result.stdout + result.stderr).strip()
            if not combined:
                raise RuntimeError(
                    f"codex exec produced no output (rc={result.returncode}).\n"
                    f"stderr: {result.stderr[:500]}"
                )
            return combined

        except FileNotFoundError:
            raise RuntimeError(
                "codex not found. Install: sudo npm install -g @openai/codex"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("codex exec timed out after 300s")
        finally:
            output_file.unlink(missing_ok=True)

    # ── OpenAI SDK backend (fallback) ──────────────────────────────────────────

    def _call_openai(self, user_prompt: str) -> str:
        """Direct OpenAI API via Python SDK. Falls back through models."""
        try:
            from openai import OpenAI, AuthenticationError, PermissionDeniedError
        except ImportError:
            raise RuntimeError("pip install openai")

        client = OpenAI(api_key=self.api_key)
        fallback_models = [self.model, "o4-mini", "gpt-4o"]
        last_err = None

        for model in fallback_models:
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    max_completion_tokens=self.max_tokens,
                )
                if model != self.model:
                    print(f"[sampler] fell back to model '{model}'")
                return resp.choices[0].message.content or ""
            except (AuthenticationError, PermissionDeniedError) as e:
                last_err = e
                continue
            except Exception:
                raise

        raise RuntimeError(f"All models failed. Last: {last_err}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _load_knowledge(self, keys: list[str]) -> str:
        docs = list(KNOWLEDGE_DIR.rglob("*.md")) if not keys else [
            p for k in keys for p in KNOWLEDGE_DIR.rglob(f"*{k}*")
        ]
        if not docs:
            return "(No knowledge documents loaded)"
        parts = []
        for doc in sorted(docs):
            rel = doc.relative_to(KNOWLEDGE_DIR)
            parts.append(f"=== {rel} ===\n{doc.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_cpp(raw: str) -> str:
        """Strip markdown fences if present; otherwise return raw."""
        m = re.search(r"```(?:cpp|c\+\+)?\s*\n(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else raw.strip()

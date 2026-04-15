"""
alphaevolve/sampler.py
──────────────────────
Interfaces with the AI backend to generate C++ scheduling algorithm implementations.

Supports two backends:
  1. Codex CLI  — `codex` npm package (uses education/ChatGPT subscription auth)
                  Works WITHOUT a paid API key if `codex auth login` was run.
  2. OpenAI SDK — direct API call via Python (requires OPENAI_API_KEY with credits)

Codex CLI approach (non-interactive):
  We write the full prompt to a temp file, then instruct codex to read it and
  write ONLY C++ code to a second temp file. This is the native file-operation
  workflow that Codex CLI is designed for, making it fully non-interactive.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


PROMPTS_DIR = Path(__file__).parent / "prompts"
KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"


class Sampler:
    """Generates C++ scheduling algorithm implementations via AI."""

    def __init__(
        self,
        backend: str = "codex",   # 'codex' | 'openai'
        model: str = "o3",
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
        current_function_source: str,
        sdc_scheduler_source: str,
        best_score: float,
        best_num_stages: int,
        best_reg_bits: int,
        best_delay_ps: int,
        parent_score: float,
        parent_num_stages: int,
        parent_reg_bits: int,
        parent_delay_ps: int,
        knowledge_keys: list[str] | None = None,
    ) -> str:
        """
        Generate a new C++ implementation for mutation_target.
        Returns the raw C++ code string (ready to splice into the source file).
        """
        knowledge_context = self._load_knowledge(knowledge_keys or [])

        template = self._jinja.get_template("implement.txt")
        user_prompt = template.render(
            mutation_target=mutation_target,
            mutation_instruction=mutation_instruction,
            current_function_source=current_function_source,
            sdc_scheduler_source=sdc_scheduler_source,
            best_score=best_score,
            best_num_stages=best_num_stages,
            best_reg_bits=best_reg_bits,
            best_delay_ps=best_delay_ps,
            parent_score=parent_score,
            parent_num_stages=parent_num_stages,
            parent_reg_bits=parent_reg_bits,
            parent_delay_ps=parent_delay_ps,
            knowledge_context=knowledge_context,
        )

        if self.backend == "codex":
            raw = self._call_codex(user_prompt)
        else:
            raw = self._call_openai(user_prompt)

        return self._extract_cpp(raw)

    # ── Codex CLI backend (primary) ────────────────────────────────────────────

    def _call_codex(self, user_prompt: str) -> str:
        """
        Call the @openai/codex npm CLI non-interactively using a file-based workflow:

        1. Write the full prompt to /tmp/alphaevolve_prompt_{pid}.txt
        2. Write expected output path to /tmp/alphaevolve_output_{pid}.cpp
        3. Ask codex to read the prompt and write C++ to the output file
        4. Read and return the output file

        This works with:
          - Education/ChatGPT subscription (codex auth login)
          - API key via OPENAI_API_KEY env var

        The file approach avoids TTY issues and shell escaping problems with
        large C++ code prompts.
        """
        pid = os.getpid()
        prompt_file = Path(f"/tmp/alphaevolve_prompt_{pid}.txt")
        output_file = Path(f"/tmp/alphaevolve_output_{pid}.cpp")

        try:
            # Write full prompt to file
            full_prompt = f"{self._system_prompt}\n\n{user_prompt}"
            prompt_file.write_text(full_prompt, encoding="utf-8")

            # Remove any stale output file
            if output_file.exists():
                output_file.unlink()

            # Codex instruction: read prompt, write C++ to output file
            codex_task = (
                f"Read the file {prompt_file} carefully. "
                f"It contains a request to implement a C++ scheduling algorithm function. "
                f"Write ONLY the complete, valid C++ implementation to {output_file}. "
                f"No prose, no markdown, no ``` fences — pure C++ source code only."
            )

            env = {**os.environ}
            if self.api_key:
                env["OPENAI_API_KEY"] = self.api_key

            result = subprocess.run(
                [
                    "codex",
                    "--model", self.model,
                    "--approval-mode", "full-auto",
                    "--quiet",
                    codex_task,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )

            # Prefer the output file (codex wrote C++ there)
            if output_file.exists() and output_file.stat().st_size > 0:
                return output_file.read_text(encoding="utf-8")

            # Fallback: parse C++ out of stdout/stderr
            combined = result.stdout + result.stderr
            if result.returncode != 0 and not combined.strip():
                raise RuntimeError(
                    f"codex exited with code {result.returncode} and no output.\n"
                    f"stderr: {result.stderr[:500]}\n"
                    "Hint: try running `codex auth login` in WSL to authenticate."
                )

            return combined

        except FileNotFoundError:
            raise RuntimeError(
                "Codex CLI not found. Install with: npm install -g @openai/codex\n"
                "Then authenticate: codex auth login"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Codex CLI timed out after 300s")
        finally:
            prompt_file.unlink(missing_ok=True)
            output_file.unlink(missing_ok=True)

    # ── OpenAI SDK backend (fallback) ──────────────────────────────────────────

    def _call_openai(self, user_prompt: str) -> str:
        """
        Call OpenAI API directly via Python SDK.
        Requires OPENAI_API_KEY with sufficient credits.
        Falls back through models if the primary is not accessible.
        """
        try:
            from openai import OpenAI, AuthenticationError, PermissionDeniedError
        except ImportError:
            raise RuntimeError("openai package not installed. Run: pip install openai")

        client = OpenAI(api_key=self.api_key)

        # Try primary model, fall back to more accessible ones
        model_fallback = [self.model, "o4-mini", "gpt-4o"]
        last_error = None

        for model in model_fallback:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    max_completion_tokens=self.max_tokens,
                )
                if model != self.model:
                    print(f"[sampler] Note: fell back to model '{model}' ('{self.model}' not accessible)")
                return response.choices[0].message.content or ""
            except (AuthenticationError, PermissionDeniedError) as e:
                last_error = e
                continue
            except Exception as e:
                # Non-auth error — don't retry with different model
                raise

        raise RuntimeError(
            f"All models failed. Last error: {last_error}\n"
            "Tip: switch to --backend codex and run `codex auth login` in WSL."
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _load_knowledge(self, keys: list[str]) -> str:
        """Load and concatenate knowledge base documents."""
        if not keys:
            docs = list(KNOWLEDGE_DIR.rglob("*.md"))
        else:
            docs = []
            for key in keys:
                docs.extend(KNOWLEDGE_DIR.rglob(f"*{key}*"))

        if not docs:
            return "(No knowledge documents loaded)"

        parts = []
        for doc in sorted(docs):
            rel = doc.relative_to(KNOWLEDGE_DIR)
            parts.append(f"=== {rel} ===\n{doc.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_cpp(raw: str) -> str:
        """
        Extract C++ code from AI response.
        Handles fenced blocks and raw C++ output.
        """
        # Try fenced code blocks
        fence_match = re.search(
            r"```(?:cpp|c\+\+)?\s*\n(.*?)```",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        if fence_match:
            return fence_match.group(1).strip()

        # Raw C++ — return as-is
        return raw.strip()

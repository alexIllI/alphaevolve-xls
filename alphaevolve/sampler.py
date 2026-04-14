"""
alphaevolve/sampler.py
──────────────────────
Interfaces with the AI backend to generate C++ scheduling algorithm implementations.

Supports two backends:
  1. Codex CLI  — `codex` subprocess (interactive, stateful)
  2. OpenAI SDK — direct API call (faster for batch, good fallback)

The sampler:
  1. Loads the system prompt and the Jinja2 implement.txt template
  2. Renders the prompt with the current evolution context
  3. Calls the AI and extracts the C++ code from the response
  4. Returns the generated code string
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


PROMPTS_DIR = Path(__file__).parent / "prompts"
KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"


class Sampler:
    """Generates C++ scheduling algorithm implementations via AI."""

    def __init__(
        self,
        backend: str = "openai",   # 'openai' | 'codex'
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

    # ── Backend implementations ────────────────────────────────────────────────

    def _call_openai(self, user_prompt: str) -> str:
        """Call OpenAI API directly via Python SDK."""
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai package not installed. Run: pip install openai")

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_completion_tokens=self.max_tokens,
        )
        return response.choices[0].message.content or ""

    def _call_codex(self, user_prompt: str) -> str:
        """
        Call Codex CLI via subprocess.
        Codex CLI is invoked in non-interactive (quiet) mode.
        """
        full_prompt = f"{self._system_prompt}\n\n{user_prompt}"
        try:
            result = subprocess.run(
                ["codex", "--model", self.model, "--quiet", full_prompt],
                capture_output=True,
                text=True,
                timeout=300,
                env={**os.environ, "OPENAI_API_KEY": self.api_key},
            )
        except FileNotFoundError:
            raise RuntimeError(
                "Codex CLI not found. Install with: npm install -g @openai/codex"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Codex CLI timed out after 300s")

        if result.returncode != 0:
            raise RuntimeError(
                f"Codex CLI failed (rc={result.returncode}):\n{result.stderr}"
            )
        return result.stdout

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _load_knowledge(self, keys: list[str]) -> str:
        """Load and concatenate knowledge base documents."""
        if not keys:
            # Load all available knowledge files
            docs = list(KNOWLEDGE_DIR.rglob("*.md"))
        else:
            docs = []
            for key in keys:
                candidates = list(KNOWLEDGE_DIR.rglob(f"*{key}*"))
                docs.extend(candidates)

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
        Extract C++ code from the AI response.
        Handles:
          - Raw C++ (no fences)
          - ```cpp ... ``` fences
          - ``` ... ``` fences
        """
        # Try fenced code blocks first
        fence_match = re.search(
            r"```(?:cpp|c\+\+)?\s*\n(.*?)```",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        if fence_match:
            return fence_match.group(1).strip()

        # If no fences, assume the whole response is C++ (as instructed)
        return raw.strip()

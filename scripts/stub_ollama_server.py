"""
Offline Ollama stand-in for exercising the experiment harness pipeline.

THIS IS NOT A LANGUAGE MODEL. It emulates the three Ollama endpoints that
Local Corvus uses, with deterministic, retrieval-aware canned behavior:

  - GET  /api/tags             -> reports the configured model as available
                                  (backend /ollama-health + harness judge preflight)
  - POST /v1/chat/completions  -> "agent" turns: composes a response from the
                                  memory context the backend injected, so
                                  retrieval quality flows through to response
                                  quality exactly along the pipeline's real
                                  causal path (backend orchestrator + extractor)
  - POST /api/chat             -> "judge" calls: parses the harness judge
                                  prompt and returns a closed-vocabulary
                                  up/down/none verdict JSON derived from
                                  content-word overlap between the response
                                  and the prompt's criteria

Purpose: environments (sandboxed CI, cloud sessions) where the real
`gemma4:e4b` weights cannot be served because model registries
(ollama.com, huggingface.co, kaggle.com) are blocked by egress policy.
The stub lets the full harness -> backend -> graph -> judge -> report loop
run end-to-end so pipeline defects can be found at real scale.

An experiment executed against this stub is a PIPELINE VALIDATION, not a
science run. Reports produced this way say nothing about model learning and
must never be quoted as experimental results.

Usage (host-side, same place the real Ollama would run):
    python scripts/stub_ollama_server.py [--port 11434] [--model gemma4:e4b]

Everything is stdlib-only and deterministic: the same request bytes always
produce the same response, so a resumed harness run replays identically.
"""

import argparse
import hashlib
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = "gemma4:e4b"

_STOP_WORDS = {
    "that", "this", "with", "must", "every", "when", "which", "their", "them",
    "they", "have", "will", "should", "would", "could", "about", "because",
    "into", "from", "your", "what", "where", "does", "these", "those", "than",
    "then", "each", "also", "only", "ever", "never", "always", "both", "such",
    "being", "been", "were", "please", "mention", "mentions", "explain",
    "describe", "response", "answer", "question", "assistant",
}


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP_WORDS}


def _stable_hash(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _split_sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]


# ---------------------------------------------------------------------------
# Agent behavior (/v1/chat/completions)
# ---------------------------------------------------------------------------

_CTX_START = "--- Context from memory ---"
_CTX_END = "--- End context from memory ---"


def _memory_context(messages: list) -> str:
    """Extract only the retrieved-memory block the backend injected into the
    system role — instruction boilerplate around it is not answer material."""
    for m in messages:
        content = m.get("content", "")
        if m.get("role") == "system" and isinstance(content, str) and _CTX_START in content:
            block = content.split(_CTX_START, 1)[1]
            return block.split(_CTX_END, 1)[0].strip()
    return ""


def build_agent_response(messages: list) -> str:
    """
    Deterministic 'agent' turn: answer using whatever memory context the
    backend injected. The retrieved context is echoed back — sentences that
    lexically overlap the question first, then the context's own leading
    sentences — so retrieval quality is what the judge ends up grading
    (the pipeline's real causal path). With no retrieved context the answer
    is deliberately vague. A stable ~15% slice of prompts gets a low-effort
    answer regardless, so 'down'/'none' verdicts occur and the negative
    weight-update path gets exercised too.
    """
    context_text = _memory_context(messages)
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            user_text = m["content"]
            break

    seed = _stable_hash(user_text + "|" + context_text)
    question_words = _content_words(user_text)

    # Stable low-effort slice: ignores context, gives a filler answer.
    if seed % 100 < 15:
        return (
            "That's an interesting question. There are many ways to think about "
            "this topic and it often depends on the situation. I would suggest "
            "considering the trade-offs carefully before deciding."
        )

    def _usable(sentence: str) -> bool:
        # The extractor's condensed context can include the current turn's
        # own input MU (the question itself) and, on a graph carrying prior
        # prompt MUs, nested context-marker boilerplate. Neither is answer
        # material — echoing them back just parrots the question.
        if "---" in sentence or "retrieved context from your memory store" in sentence:
            return False
        if sentence in user_text or user_text in sentence:
            return False
        s_words = _content_words(sentence)
        if s_words and len(s_words & question_words) / len(s_words) > 0.8:
            return False
        return True

    sentences = [s for s in _split_sentences(context_text) if _usable(s)]
    if sentences:
        ranked = sorted(
            range(len(sentences)),
            key=lambda i: (-len(_content_words(sentences[i]) & question_words), i),
        )
        chosen = [sentences[i] for i in sorted(ranked[:3])]
        return (
            "Based on what I have stored about this, the key points are as follows. "
            + " ".join(chosen)
            + " In short, following these established practices is what keeps the "
            "process dependable."
        )

    # No usable retrieved context — hedge, but echo the question's own terms
    # so the output is still deterministic per prompt.
    echo = ", ".join(sorted(question_words)[:4]) or "this topic"
    return (
        f"I don't have specific stored guidance about {echo}. Speaking generally, "
        "it helps to define expectations up front, keep conventions consistent, "
        "and verify results at each step."
    )


# ---------------------------------------------------------------------------
# Judge behavior (/api/chat)
# ---------------------------------------------------------------------------

_CRITERIA_HEADER = "Criteria the response should satisfy:"
_RESPONSE_HEADER = "Assistant's response:"
_DECIDE_MARKER = "Decide whether the response satisfies"


def _parse_judge_prompt(prompt: str) -> tuple:
    """Extract (criteria list, response text) from the harness judge template."""
    criteria = []
    if _CRITERIA_HEADER in prompt:
        tail = prompt.split(_CRITERIA_HEADER, 1)[1]
        block = tail.split(_RESPONSE_HEADER, 1)[0]
        criteria = [
            line[2:].strip() for line in block.splitlines() if line.startswith("- ")
        ]
    response_text = ""
    if _RESPONSE_HEADER in prompt:
        response_text = prompt.split(_RESPONSE_HEADER, 1)[1]
        if _DECIDE_MARKER in response_text:
            response_text = response_text.split(_DECIDE_MARKER, 1)[0]
    return criteria, response_text.strip()


def build_judge_verdict(prompt: str) -> dict:
    """
    Closed-vocabulary verdict from content-word overlap between the graded
    response and each criterion. Deterministic; 'down' always carries the
    exact text of one listed criterion (the harness validates this).
    """
    criteria, response_text = _parse_judge_prompt(prompt)
    if not criteria or not response_text:
        return {
            "verdict": "none",
            "rationale": "stub judge could not locate criteria/response in prompt",
            "criterion_triggered": None,
        }

    response_words = _content_words(response_text)
    unsatisfied = []
    satisfied = 0
    for criterion in criteria:
        c_words = _content_words(criterion)
        overlap = len(c_words & response_words)
        if c_words and (overlap >= 2 or overlap >= len(c_words) * 0.5):
            satisfied += 1
        else:
            unsatisfied.append(criterion)

    frac = satisfied / len(criteria)
    seed = _stable_hash(response_text)

    if frac >= 0.99:
        # Small stable 'none' slice keeps the ambiguous path exercised.
        if seed % 20 == 0:
            return {
                "verdict": "none",
                "rationale": "stub judge: satisfied but flagged ambiguous (stable slice)",
                "criterion_triggered": None,
            }
        return {
            "verdict": "up",
            "rationale": f"stub judge: {satisfied}/{len(criteria)} criteria matched",
            "criterion_triggered": None,
        }
    if frac >= 0.5:
        return {
            "verdict": "none",
            "rationale": f"stub judge: only {satisfied}/{len(criteria)} criteria matched",
            "criterion_triggered": None,
        }
    return {
        "verdict": "down",
        "rationale": f"stub judge: {satisfied}/{len(criteria)} criteria matched",
        "criterion_triggered": unsatisfied[0],
    }


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class StubOllamaHandler(BaseHTTPRequestHandler):
    model_name = MODEL_NAME

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet: one line per request is enough
        print(f"[stub-ollama] {self.command} {self.path}")

    def do_GET(self):
        if self.path == "/api/tags":
            self._send_json({
                "models": [{
                    "name": self.model_name,
                    "model": self.model_name,
                    "modified_at": "2026-07-05T00:00:00Z",
                    "size": 0,
                    "details": {"family": "stub", "parameter_size": "0B"},
                }]
            })
        elif self.path == "/":
            body = b"Ollama is running (STUB)"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"error": f"unknown path {self.path}"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON body"}, status=400)
            return

        if self.path == "/v1/chat/completions":
            content = build_agent_response(payload.get("messages", []))
            prompt_chars = sum(
                len(m.get("content", "")) for m in payload.get("messages", [])
                if isinstance(m.get("content"), str)
            )
            self._send_json({
                "id": "stub-" + hashlib.sha256(content.encode()).hexdigest()[:12],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model", self.model_name),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": max(1, prompt_chars // 4),
                    "completion_tokens": max(1, len(content) // 4),
                    "total_tokens": max(2, (prompt_chars + len(content)) // 4),
                },
            })
        elif self.path == "/api/chat":
            user_prompt = ""
            for m in payload.get("messages", []):
                if m.get("role") == "user":
                    user_prompt = m.get("content", "")
            verdict = build_judge_verdict(user_prompt)
            self._send_json({
                "model": payload.get("model", self.model_name),
                "created_at": "2026-07-05T00:00:00Z",
                "message": {"role": "assistant", "content": json.dumps(verdict)},
                "done": True,
            })
        else:
            self._send_json({"error": f"unknown path {self.path}"}, status=404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ollama stand-in (pipeline validation only)")
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--model", type=str, default=MODEL_NAME)
    args = parser.parse_args()

    StubOllamaHandler.model_name = args.model
    server = ThreadingHTTPServer(("0.0.0.0", args.port), StubOllamaHandler)
    print(f"[stub-ollama] serving on 0.0.0.0:{args.port} as {args.model!r} — NOT A REAL MODEL")
    server.serve_forever()


if __name__ == "__main__":
    main()

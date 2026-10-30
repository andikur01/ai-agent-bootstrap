"""Structured reasoning for /pikir.

A single "think harder" prompt is not reasoning, it is just a longer answer. This
module runs an explicit multi-pass loop with a visible plan, so the user can see
where the conclusion came from:

1. FRAME   - restate the problem, list what is known vs unknown
2. PLAN    - enumerate candidate approaches and pick one with a reason
3. WORK   - step through the chosen approach
4. CHECK  - actively try to falsify the conclusion
5. ANSWER - final answer, confidence, and what would change it

Each pass is a separate cheap call, and the transcript of earlier passes is fed
forward compactly. Intermediate passes use the small tier; only the final answer
uses the big tier.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging_setup import get_logger

log = get_logger(__name__)

PASS_PROMPTS = {
    "frame": (
        "PASS 1 - FRAME. Rumuskan ulang pertanyaan intinya dalam 1 kalimat. "
        "Lalu pisahkan: FAKTA yang diketahui, ASUMSI yang kamu pakai, dan "
        "YANG BELUM DIKETAHUI. Jujur soal yang tidak diketahui. Maksimal 200 kata."
    ),
    "plan": (
        "PASS 2 - PLAN. Sebutkan 2-3 pendekatan berbeda untuk menjawab. "
        "Untuk masing-masing: satu kalimat isi, satu kalimat risiko/kelemahan. "
        "Lalu pilih satu dan jelaskan alasannya dalam 1-2 kalimat. Maksimal 250 kata."
    ),
    "work": (
        "PASS 3 - WORK. Kerjakan pendekatan yang dipilih, langkah demi langkah. "
        "Tulis langkah bernomor. Tunjukkan perhitungan atau alasan konkret, "
        "bukan kesimpulan saja. Maksimal 400 kata."
    ),
    "check": (
        "PASS 4 - CHECK. Serang kesimpulanmu sendiri. Sebutkan minimal 2 hal yang "
        "bisa membuat jawaban ini salah, dan sebutkan bagian mana yang paling lemah. "
        "Kalau ada kesalahan di PASS 3, koreksi di sini. Maksimal 250 kata."
    ),
}

FINAL_PROMPT = (
    "PASS 5 - ANSWER. Tulis jawaban akhir yang langsung menjawab pertanyaan user. "
    "Pakai semua koreksi dari PASS 4. Format:\n"
    "**Jawaban**: <jawaban langsung, 2-5 kalimat>\n"
    "**Alasan utama**: <2-4 poin>\n"
    "**Keyakinan**: <tinggi/sedang/rendah> karena <alasan>\n"
    "**Yang bisa mengubah jawaban ini**: <1-2 poin>\n"
    "Jangan ulangi seluruh proses berpikir. Maksimal 350 kata."
)


@dataclass
class Thought:
    passes: dict[str, str] = field(default_factory=dict)
    answer: str = ""
    tokens: int = 0

    def transcript(self, max_chars: int = 6000) -> str:
        blocks = []
        used = 0
        for name in ("frame", "plan", "work", "check"):
            body = self.passes.get(name, "")
            if not body:
                continue
            entry = f"### {name.upper()}\n{body}"
            if used + len(entry) > max_chars:
                break
            blocks.append(entry)
            used += len(entry)
        return "\n\n".join(blocks)

    def show(self, *, include_passes: bool = True) -> str:
        if not include_passes:
            return self.answer
        return f"{self.transcript(3500)}\n\n---\n\n{self.answer}"


class ThinkingEngine:
    def __init__(self, gateway, *, tier_small: str = "small", tier_final: str = "big"):
        self.gateway = gateway
        self.tier_small = tier_small
        self.tier_final = tier_final

    async def think(
        self,
        question: str,
        *,
        user_id: int = 0,
        context: str = "",
        progress=None,
    ) -> Thought:
        thought = Thought()
        base = [
            {
                "role": "system",
                "content": (
                    "Kamu pemikir analitis yang teliti dan jujur. Kamu mengerjakan "
                    "satu pass penalaran pada satu waktu, sesuai instruksi pass. "
                    "Jangan melompat ke jawaban akhir sebelum diminta."
                ),
            }
        ]
        context_block = f"\n\nKonteks relevan:\n{context}" if context else ""

        for index, (name, instruction) in enumerate(PASS_PROMPTS.items(), start=1):
            if progress:
                await progress(int(10 + 60 * index / 5), f"Pass {index}/5: {name}")
            messages = [
                *base,
                {
                    "role": "user",
                    "content": (
                        f"Pertanyaan user: {question}{context_block}\n\n"
                        f"{instruction}"
                    ),
                },
            ]
            if thought.passes:
                messages.insert(
                    1,
                    {
                        "role": "assistant",
                        "content": "Hasil pass sebelumnya:\n\n" + thought.transcript(3000),
                    },
                )
            try:
                result = await self.gateway.complete(
                    messages,
                    tier=self.tier_small,
                    user_id=user_id,
                    temperature=0.3,
                    max_tokens=900,
                )
                thought.passes[name] = result.text.strip()
                thought.tokens += result.total_tokens
            except Exception as exc:  # noqa: BLE001
                log.warning("thinking pass %s failed: %s", name, exc)
                thought.passes[name] = f"(pass {name} gagal: {exc})"

        if progress:
            await progress(80, "Pass 5/5: menyusun jawaban akhir")
        try:
            result = await self.gateway.complete(
                [
                    *base,
                    {
                        "role": "user",
                        "content": (
                            f"Pertanyaan user: {question}{context_block}\n\n"
                            f"Hasil penalaran:\n{thought.transcript(7000)}\n\n"
                            f"{FINAL_PROMPT}"
                        ),
                    },
                ],
                tier=self.tier_final,
                user_id=user_id,
                temperature=0.2,
                max_tokens=1200,
            )
            thought.answer = result.text.strip()
            thought.tokens += result.total_tokens
        except Exception as exc:  # noqa: BLE001
            log.warning("final answer pass failed: %s", exc)
            thought.answer = (
                "Aku gagal menyusun jawaban akhir.\n\n"
                + thought.passes.get("work", "")[:1500]
            )
        return thought

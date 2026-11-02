"""The orchestrator: one place where a user message becomes an action.

Flow for every incoming message:

    route -> resolve reference -> confirm (heavy only) -> execute -> remember

Design decisions worth stating:

* Heavy work always passes through a confirmation gate with a token estimate.
  The user is told the cost *before* paying it.
* Chat answers go through the semantic cache first, then memory-backed context.
* Every outcome (artifact, error, learning) is written to memory, so the next
  "perbaiki yang tadi" resolves to the real thing.
* Failures are recorded to the error log with the module name, which is what
  makes "jangan ulangi kesalahan yang sama" enforceable rather than aspirational.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Awaitable, Callable

from app.core.errors import AgentError, MissingSecretError, TaskCancelledError
from app.core.intent import Intent, Route, route_message
from app.core.logging_setup import get_logger
from app.core.specialists import Specialist, for_intent as specialist_for, pick as pick_specialists
from app.core.thinking import ThinkingEngine
from app.llm.tokens import count_message_tokens, estimate
from app.memory.artifacts import (
    KIND_AUDIO,
    KIND_IMAGE,
    KIND_LINK,
    KIND_N8N,
    KIND_RESEARCH,
    KIND_VIDEO,
    KIND_WEB,
    KIND_WEB_APP,
)
from app.services.audio_service import AudioService
from app.services.browser_agent import BrowserAgent
from app.services.browser_qa import BrowserQA
from app.services.deploy_service import DeployService
from app.services.image_service import ImageService
from app.services.link_service import LinkIngestService, answer_about_link
from app.services.n8n_service import N8nService
from app.services.search_service import SearchService, learn_topic
from app.services.scheduler_service import parse_reminder
from app.services.video_service import VideoService
from app.services.web_builder import WebBuilder
from app.services.web_repair import WebRepairService

log = get_logger(__name__)

# Personas now live in app.core.specialists, one per kind of work, so a website
# is never written with a chat assistant's habits.

CONFIRM_TEMPLATES = {
    Intent.IMAGE_CREATE: ("🎨 Bikin gambar baru", "image"),
    Intent.IMAGE_EDIT: ("🖌️ Edit gambar", "image"),
    Intent.VIDEO_CREATE: ("🎬 Bikin video", "video"),
    Intent.VIDEO_EDIT: ("✂️ Edit video", "video"),
    Intent.AUDIO_CREATE: ("🎵 Bikin audio", "audio"),
    Intent.AUDIO_EDIT: ("🎚️ Edit audio", "audio"),
    Intent.WEB_STATIC: ("🌐 Bikin website", "web"),
    Intent.WEB_FULLSTACK: ("🧩 Bikin web app (login + database)", "web"),
    Intent.WEB_REPAIR: ("🔧 Perbaiki website", "web"),
    Intent.N8N: ("⚙️ Bikin workflow n8n", "n8n"),
    Intent.LEARN: ("📚 Riset dan simpan ke memori", "research"),
    Intent.LINK_INGEST: ("🔗 Baca isi link", "link"),
    Intent.THINK: ("🧠 Analisis mendalam (5 pass)", "think"),
}

# Honest ranges, not point estimates. A single number implies a precision
# nobody has before the model has seen the request: the same "buat web" can cost
# 4k or 30k depending on how many files and repair rounds it needs. A range plus
# the real total after the run is more useful than a confident wrong figure.
TOKEN_ESTIMATES = {
    Intent.IMAGE_CREATE: (600, 1500),
    Intent.IMAGE_EDIT: (600, 1500),
    Intent.VIDEO_CREATE: (1500, 6000),
    Intent.VIDEO_EDIT: (200, 800),
    Intent.AUDIO_CREATE: (300, 900),
    Intent.AUDIO_EDIT: (100, 400),
    Intent.WEB_STATIC: (4000, 20000),
    Intent.WEB_FULLSTACK: (6000, 30000),
    Intent.WEB_REPAIR: (5000, 25000),
    Intent.N8N: (1500, 6000),
    Intent.LEARN: (2000, 8000),
    Intent.LINK_INGEST: (3000, 12000),
    Intent.THINK: (3000, 12000),
    Intent.CHAT: (300, 1200),
}


def estimate_tokens_for(intent) -> int:
    """One number for a task, not a range.

    TOKEN_ESTIMATES holds a (low, high) pair for the confirmation prompt, but
    Outcome.tokens is formatted as a single figure. Using the pair directly
    crashed the whole task with "unsupported format string passed to
    tuple.__format__" - found in the live log, not in a test. The midpoint is
    the honest single number to report after the fact.
    """
    low, high = TOKEN_ESTIMATES.get(intent, (300, 1200))
    return (low + high) // 2


@dataclass
class Outcome:
    """What the orchestrator produced, ready for the Telegram layer to render."""

    text: str = ""
    files: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    url: str = ""
    needs_confirm: bool = False
    confirm_text: str = ""
    pending_route: Route | None = None
    skipped_qa: bool = False
    tokens: int = 0
    cached: bool = False
    milestone_screenshots: list[str] = field(default_factory=list)
    specialist: str = ""


class Orchestrator:
    def __init__(
        self,
        *,
        settings,
        store,
        gateway,
        memory,
        cache,
        budget,
        scheduler=None,
        profiles=None,
        vault=None,
        live_view=None,
    ):
        self.settings = settings
        self.store = store
        self.gateway = gateway
        self.memory = memory
        self.cache = cache
        self.budget = budget
        self.scheduler = scheduler
        self.profiles = profiles
        self.vault = vault
        self.live_view = live_view

        outputs = settings.outputs_path
        self.images = ImageService(settings, outputs_dir=outputs)
        self.audio = AudioService(settings, outputs_dir=outputs)
        self.videos = VideoService(settings, gateway=gateway, audio=self.audio, outputs_dir=outputs)
        self.search = SearchService(settings)
        self.links = LinkIngestService(settings, audio=self.audio, outputs_dir=outputs)
        self.web = WebBuilder(settings, gateway=gateway, outputs_dir=outputs)
        self.deployer = DeployService(settings)
        self.qa = BrowserQA(outputs)
        self.repair = WebRepairService(
            settings, builder=self.web, deployer=self.deployer, qa=self.qa
        )
        self.n8n = N8nService(settings, gateway=gateway, outputs_dir=outputs)
        self.thinking = ThinkingEngine(gateway)
        # The browser agent gets the saved session and the shared display, so it
        # can work behind a login and the user can watch or take over.
        self.browser = BrowserAgent(
            settings,
            gateway=gateway,
            outputs_dir=outputs,
            profiles=profiles,
            vault=vault,
            live_view=live_view,
        )

    # ------------------------------------------------------------------ route
    async def classify(
        self,
        user_id: int,
        text: str,
        *,
        has_photo: bool = False,
        has_voice: bool = False,
        has_document: bool = False,
    ) -> Route:
        return await route_message(
            text,
            self.gateway if self.gateway.available() else None,
            user_id=user_id,
            has_photo=has_photo,
            has_voice=has_voice,
            has_document=has_document,
        )

    def estimate_tokens(self, route: Route) -> tuple[int, int]:
        return TOKEN_ESTIMATES.get(route.intent, (300, 1200))

    def confirmation_prompt(self, route: Route) -> str:
        label, _ = CONFIRM_TEMPLATES.get(route.intent, ("Jalankan tugas", ""))
        low, high = self.estimate_tokens(route)
        target = ""
        if route.args.get("reference_brief"):
            target = f"\nTarget: {route.args['reference_brief']}"
        return (
            f"{label}?\n"
            f"Perkiraan token: {low:,}-{high:,}"
            f"{target}\n\n"
            "Balas *ya* untuk lanjut, *tidak* untuk batal."
        )

    # --------------------------------------------------------------- execute
    async def execute(
        self,
        user_id: int,
        route: Route,
        *,
        progress=None,
        photo_path: str = "",
        voice_path: str = "",
        document_path: str = "",
        chat_id: int = 0,
    ) -> Outcome:
        handler = getattr(self, f"_do_{route.intent.value}", None)
        if handler is None:
            handler = self._do_chat

        # The expert for this kind of work. Handlers read it for persona, tier
        # and budget, so a website is never written with a chat assistant's
        # habits. The user asked for exactly this: hand the task to the expert.
        expert = self.specialist_for_route(route)
        route.args.setdefault("specialist", expert.key)
        try:
            outcome = await handler(
                user_id,
                route,
                progress=progress,
                photo_path=photo_path,
                voice_path=voice_path,
                document_path=document_path,
                chat_id=chat_id,
            )
        except (MissingSecretError, TaskCancelledError):
            raise
        except AgentError as exc:
            await self.store.log_error(route.intent.value, str(exc), user_id)
            raise
        except Exception as exc:  # noqa: BLE001
            await self.store.log_error(route.intent.value, f"{type(exc).__name__}: {exc}", user_id)
            log.exception("handler failed intent=%s", route.intent.value)
            raise AgentError("Ada error tak terduga saat menjalankan tugas ini.") from exc
        if not outcome.specialist:
            outcome.specialist = expert.key
        return outcome

    def specialist_for_route(self, route: Route) -> Specialist:
        """The expert for this route.

        A delegation request ("kasih ke ahlinya, buat video") names its own
        domain, so it wins over the default mapping for the intent.
        """
        text = str(route.args.get("text") or "")
        chosen = route.args.get("delegate_specialist")
        if chosen:
            from app.core.specialists import by_key

            expert = by_key(str(chosen))
            if expert is not None:
                return expert
        if route.args.get("delegate"):
            options = pick_specialists(text)
            if options:
                return options[0]
        return specialist_for(route.intent)

    # ------------------------------------------------------------- continue
    async def _do_continue(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        """Resolve "lanjutkan yang tadi" to the real last artifact and act on it."""
        text = str(route.args.get("text") or "")
        reference = await self.memory.resolve_reference(user_id, text)
        artifact = reference.artifact

        if artifact is None:
            recent = await self.memory.recent_artifacts(user_id, limit=3)
            if not recent:
                return Outcome(
                    text="Aku belum punya pekerjaan sebelumnya. Mau mulai dari mana?"
                )
            artifact = recent[0]

        kind = artifact.kind
        title = artifact.title or artifact.location
        header = f"Melanjutkan *{title}* ({kind})."

        if kind == KIND_IMAGE:
            source = Path(artifact.location)
            if source.exists():
                result = await self.images.edit(
                    "perbaiki dan tingkatkan kualitas gambar ini", source=source, progress=progress
                )
                await self.memory.register_artifact(
                    user_id,
                    KIND_IMAGE,
                    title=f"lanjutan: {title[:60]}",
                    location=str(result.path),
                    meta={"from": str(source)},
                )
                return Outcome(
                    text=f"{header}\nAku perbaiki gambarnya jadi versi lebih rapi.",
                    photos=[str(result.path)],
                    tokens=100,
                )

        if kind in (KIND_WEB, KIND_WEB_APP):
            return await self._do_web_repair(user_id, route, progress=progress, **kwargs)

        if kind in (KIND_VIDEO, KIND_AUDIO):
            return Outcome(
                text=(
                    f"{header}\n"
                    f"File terakhir: {artifact.location}\n"
                    "Mau aku edit bagaimana? Contoh: 'potong jadi 10 detik' atau 'tambah subtitle'."
                )
            )

        if kind == KIND_N8N:
            return Outcome(text=f"{header}\nWorkflow terakhir ada di {artifact.location}.")

        if kind in (KIND_RESEARCH, KIND_LINK):
            question = f"Lanjutkan pembahasan: {title}"
            expert = self.specialist_for_route(route)
            bundle = await self.memory.build_context(
                user_id, question, persona=expert.persona, recent_turns=4
            )
            result = await self.gateway.complete(
                bundle.messages, tier=expert.tier, user_id=user_id, max_tokens=700
            )
            return Outcome(text=f"{header}\n\n{result.text.strip()}", tokens=result.total_tokens)

        return Outcome(
            text=(
                f"{header}\n"
                f"Lokasi: {artifact.location}\n"
                "Bilang bagian mana yang mau dilanjutkan."
            )
        )

    # ------------------------------------------------------------------ chat
    async def _do_chat(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        text = str(route.args.get("text") or "")
        if not text.strip():
            return Outcome(text="Aku di sini. Mau bikin apa?")

        # "apakah webnya sudah jadi?" is answered from the registry, in the
        # bot's own words, before any model is asked. The model would happily
        # invent a status, and a wrong "yes" is worse than no answer.
        if route.args.get("ask_status"):
            factual = await self._status_answer(user_id, text)
            if factual is not None:
                return Outcome(text=factual)

        if self.settings.semantic_cache_enabled:
            hit = await self.cache.get(user_id, text, namespace="chat")
            if hit is not None:
                if self.budget:
                    await self.budget.record(user_id, "cache", 0, 0, cached_hits=1)
                return Outcome(text=hit.response, cached=True)

        reference = await self.memory.resolve_reference(user_id, text)
        expert = self.specialist_for_route(route)
        persona = await self._persona_for(user_id, expert)
        bundle = await self.memory.build_context(
            user_id, text, persona=persona, reference=reference
        )

        if progress:
            await progress(30, "Menyusun jawaban")
        result = await self.gateway.complete(
            bundle.messages,
            tier=expert.tier,
            user_id=user_id,
            temperature=0.5,
            max_tokens=expert.max_tokens,
        )
        answer = result.text.strip()

        if self.settings.semantic_cache_enabled and len(answer) > 40:
            await self.cache.put(user_id, text, answer, namespace="chat")

        return Outcome(text=answer, tokens=result.total_tokens, specialist=expert.key)

    async def _persona_for(self, user_id: int, expert) -> str:
        """The expert's own instructions, plus the user's freedom setting.

        "Hidupkan bot" is the user's phrase for turning the assistant loose: it
        stops hedging and starts deciding. The switch is stored per user, so it
        survives restarts, and the expert persona is always kept - freedom is
        about how much it acts on its own, never about dropping its standards.
        """
        try:
            prefs = await self.store.get_prefs(user_id)
        except Exception:  # noqa: BLE001 - prefs are never worth failing a reply
            prefs = {}
        if prefs.get("free_mode"):
            from app.core.specialists import FREE_MODE_RULES

            return f"{expert.persona}\n\n{FREE_MODE_RULES}"
        return expert.persona

    async def _status_answer(self, user_id: int, text: str) -> str | None:
        """Answer a "is it done yet?" question from real artifact records.

        Returns None when nothing relevant was ever made, so the model handles
        it instead. Guessing is the thing to avoid here.
        """
        recent = await self.memory.recent_artifacts(user_id, limit=6)
        if not recent:
            return None

        wanted = _kind_from_question(text)
        if wanted:
            recent = [a for a in recent if a.kind in wanted] or recent

        lines = ["Iya, ini yang aku catat:", ""]
        for artifact in recent:
            state = "ada" if artifact.exists() else "hilang dari disk"
            lines.append(f"- {artifact.brief()} ({state})")
        lines.append("")
        lines.append("Mau aku lanjutkan yang mana?")
        return "\n".join(lines)

    # ----------------------------------------------------------------- think
    async def _do_think(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        question = str(route.args.get("text") or "").strip()
        reference = await self.memory.resolve_reference(user_id, question)
        bundle = await self.memory.build_context(user_id, question, persona="", reference=reference)
        context = bundle.system[:4000]

        thought = await self.thinking.think(
            question, user_id=user_id, context=context, progress=progress
        )
        await self.memory.remember_topic(
            user_id, thought.answer[:1500], topic=question[:120], source="thinking"
        )
        return Outcome(text=thought.show(), tokens=thought.tokens)

    # --------------------------------------------------------------- gambar
    async def _do_image_create(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        prompt = _clean_prompt(route.args.get("text") or "")
        if not prompt:
            return Outcome(
                text="Mau gambar apa? Tulis deskripsinya, contoh:\n"
                "`/buat_gambar kucing astronot di bulan, gaya cat air`"
            )
        size = _extract_size(prompt) or "1024x1024"
        result = await self.images.generate(prompt, size=size, progress=progress)
        artifact = await self.memory.register_artifact(
            user_id,
            KIND_IMAGE,
            title=prompt[:80],
            location=str(result.path),
            meta={"prompt": prompt, "provider": result.provider, "size": f"{result.width}x{result.height}"},
        )
        note = ""
        requested = _size_pair(size)
        if requested and (result.width, result.height) != requested:
            # The free tier quietly downscales, so say so instead of letting the
            # delivered size silently disagree with what was asked for.
            note = (
                f"\nCatatan: diminta {requested[0]}x{requested[1]}, "
                f"provider gratis memberi {result.width}x{result.height}."
            )
        return Outcome(
            text=f"Gambar jadi ({result.provider}, {result.width}x{result.height}).{note}",
            photos=[str(result.path)],
            files=[str(result.path)],
            tokens=900,
        )

    async def _do_image_edit(self, user_id: int, route: Route, *, progress=None, photo_path="", **kwargs) -> Outcome:
        instruction = str(route.args.get("instruction") or route.args.get("text") or "")
        source = photo_path
        if not source:
            artifact = await self.memory.latest_artifact(user_id, KIND_IMAGE)
            if artifact is None:
                return Outcome(text="Belum ada gambar yang bisa diedit. Kirim gambarnya atau minta aku bikin dulu.")
            source = artifact.location
        result = await self.images.edit(instruction, source=Path(source), progress=progress)
        await self.memory.register_artifact(
            user_id,
            KIND_IMAGE,
            title=f"edit: {instruction[:60]}",
            location=str(result.path),
            meta={"prompt": instruction, "source": str(source), "provider": result.provider},
        )
        return Outcome(
            text=f"Selesai diedit ({result.provider}).",
            photos=[str(result.path)],
            files=[str(result.path)],
            tokens=900,
        )

    # ---------------------------------------------------------------- video
    async def _do_video_create(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        request = _clean_prompt(route.args.get("text") or "")
        result = await self.videos.create(request, progress=progress)
        await self.memory.register_artifact(
            user_id,
            KIND_VIDEO,
            title=(result.spec.get("title") or request)[:80],
            location=str(result.path),
            meta={"prompt": request, "provider": result.provider, "duration": result.duration},
        )
        return Outcome(
            text=f"Video jadi ({result.provider}, {result.duration:.0f} detik).",
            files=[str(result.path)],
            tokens=2500,
        )

    async def _do_video_edit(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        instruction = str(route.args.get("text") or "")
        artifact = await self.memory.latest_artifact(user_id, KIND_VIDEO)
        if artifact is None:
            return Outcome(text="Belum ada video untuk diedit. Minta aku bikin video dulu ya.")
        result = await self.videos.edit(instruction, source=Path(artifact.location), progress=progress)
        await self.memory.register_artifact(
            user_id,
            KIND_VIDEO,
            title=f"edit: {instruction[:60]}",
            location=str(result.path),
            meta={"prompt": instruction, "source": artifact.location},
        )
        return Outcome(
            text=f"Video selesai diedit ({result.duration:.0f} detik).",
            files=[str(result.path)],
            tokens=400,
        )

    # ---------------------------------------------------------------- audio
    async def _do_audio_create(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        text = str(route.args.get("text") or "")
        # If the request is really "say this", TTS is what the user wants.
        tts = await self.audio.text_to_speech(text, progress=progress)
        await self.memory.register_artifact(
            user_id,
            KIND_AUDIO,
            title=text[:70],
            location=str(tts.path),
            meta={"provider": tts.provider, "duration": tts.duration, "kind": "tts"},
        )
        return Outcome(
            text=f"Audio jadi ({tts.duration:.1f} detik).",
            files=[str(tts.path)],
            tokens=600,
        )

    async def _do_tts(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        return await self._do_audio_create(user_id, route, progress=progress, **kwargs)

    async def _do_audio_edit(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        instruction = str(route.args.get("text") or "")
        artifact = await self.memory.latest_artifact(user_id, KIND_AUDIO)
        if artifact is None:
            return Outcome(text="Belum ada audio untuk diedit.")
        result = await self.audio.edit(instruction, source=Path(artifact.location), progress=progress)
        await self.memory.register_artifact(
            user_id,
            KIND_AUDIO,
            title=f"edit: {instruction[:60]}",
            location=str(result.path),
            meta={"source": artifact.location},
        )
        return Outcome(text="Audio selesai diedit.", files=[str(result.path)], tokens=200)

    async def _do_stt(self, user_id: int, route: Route, *, progress=None, voice_path="", **kwargs) -> Outcome:
        if not voice_path:
            artifact = await self.memory.latest_artifact(user_id, KIND_AUDIO)
            voice_path = artifact.location if artifact else ""
        if not voice_path:
            return Outcome(text="Kirim pesan suara atau file audio dulu ya.")
        if progress:
            await progress(40, "Transkripsi audio")
        transcript = await self.audio.speech_to_text(voice_path)
        question = str(route.args.get("question") or route.args.get("text") or "").strip()
        if question:
            expert = self.specialist_for_route(route)
            result = await self.gateway.complete(
                [
                    {"role": "system", "content": expert.persona},
                    {"role": "user", "content": f"Transkrip suara user:\n{transcript}\n\nPertanyaan/instruksi: {question}"},
                ],
                tier=expert.tier,
                user_id=user_id,
                temperature=0.4,
            )
            return Outcome(text=f"📝 Transkrip:\n{transcript}\n\n{result.text.strip()}", tokens=result.total_tokens)
        return Outcome(text=f"📝 Transkrip:\n{transcript}", tokens=300)

    # ------------------------------------------------------------------ web
    async def _do_web_static(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        request = _clean_prompt(route.args.get("text") or "")
        project = await self.web.build(request, kind="static", progress=progress)
        issues = project.issues()
        if issues:
            log.info("static build issues: %s", issues)
            project = await self.web.repair(project, issues, progress=progress)
        shipped = await self.repair.ship(
            project, project_name=project.title, progress=progress, fill_forms=False
        )
        return await self._web_outcome(user_id, shipped, request)

    async def _do_web_fullstack(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        request = _clean_prompt(route.args.get("text") or "")
        project = await self.web.build(request, kind="fullstack", progress=progress)
        env_vars = {
            "SUPABASE_URL": self.settings.supabase_url,
            "SUPABASE_ANON_KEY": self.settings.supabase_anon_key,
            "SUPABASE_SERVICE_ROLE_KEY": self.settings.supabase_service_role_key,
        }
        shipped = await self.repair.ship(
            project,
            project_name=project.title,
            env_vars=env_vars,
            progress=progress,
            fill_forms=True,
        )
        outcome = await self._web_outcome(user_id, shipped, request, kind=KIND_WEB_APP)
        if project.test_account:
            email = project.test_account.get("email", "")
            password = project.test_account.get("password", "")
            if email:
                outcome.text += f"\n\nAkun test: `{email}` / `{password}`"
        if project.tables:
            outcome.text += "\n\nTabel Supabase yang perlu dibuat: " + ", ".join(
                f"`{t}`" for t in project.tables
            )
        return outcome

    async def _do_web_repair(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        instruction = str(route.args.get("text") or "")
        artifact = await self.memory.latest_artifact(user_id, KIND_WEB)
        if artifact is None:
            artifact = await self.memory.latest_artifact(user_id, KIND_WEB_APP)
        if artifact is None:
            return Outcome(text="Belum ada website yang bisa diperbaiki.")
        return Outcome(
            text=(
                f"Aku punya website terakhir: {artifact.title or artifact.location}\n"
                "Kirim URL-nya (atau bilang 'perbaiki yang tadi') dan aku jalankan "
                "QA + perbaikan otomatis."
            ),
        )

    # ---------------------------------------------------------------- browse
    async def _do_browse(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        """Open a URL in a real browser and carry out the user's instruction."""
        text = str(route.args.get("text") or "")
        url = _extract_url(text)
        task = _strip_url(text) or "Lihat halaman ini dan laporkan isinya."

        if not url:
            artifact = await self.memory.latest_artifact(user_id, KIND_WEB)
            if artifact is not None and str(artifact.location).startswith("http"):
                url = str(artifact.location)
            else:
                return Outcome(
                    text=(
                        "Kirim URL yang mau aku buka, contoh:\n"
                        "`/browse https://contoh.com cari harga tiket kereta`"
                    )
                )

        if progress:
            await progress(5, f"Membuka {url}")

        run = await self.browser.run(url, task, user_id=user_id, progress=progress)

        if not run.ok and run.error:
            return Outcome(
                text=f"Gagal membuka situs itu.\n`{run.error}`",
                tokens=200,
            )

        steps = "\n".join(f"{s}" for s in run.steps[:10])
        body = run.answer.strip() or "Selesai, tapi tidak ada jawaban yang bisa aku rangkum."
        text_out = f"*Browser agent* selesai di `{url}`\n\n{body}"
        if steps:
            text_out += f"\n\nLangkah:\n{steps}"

        await self.memory.register_artifact(
            user_id,
            KIND_LINK,
            title=f"browse: {task[:60]}",
            location=url,
            meta={"task": task, "steps": run.steps[:10], "ok": run.ok},
        )
        shots = [s for s in run.screenshots if s][-2:]
        produced, note = self._collect_download()
        text_out += note
        return Outcome(
            text=text_out,
            url=url,
            files=produced,
            milestone_screenshots=shots,
            tokens=estimate_tokens_for(Intent.THINK),
        )

    def _collect_download(self) -> tuple[list[str], str]:
        """Take the file the browser agent downloaded, if there is one.

        A tool site - the kind that makes video or images - ends by handing the
        user a download. That file *is* the result, so returning only a summary
        would leave the user with nothing usable. Clearing it afterwards stops
        the same file being sent twice.
        """
        downloaded = getattr(self.browser, "downloaded", None)
        if downloaded is None:
            return [], ""
        path = Path(downloaded)
        if not path.exists():
            self.browser.downloaded = None
            return [], ""
        self.browser.downloaded = None
        return [str(path)], f"\n\n📥 Hasil: `{path.name}`"

    async def _web_outcome(self, user_id: int, shipped, request: str, kind: str = KIND_WEB) -> Outcome:
        location = shipped.deployment.url if shipped.deployment else str(shipped.project.root)
        await self.memory.register_artifact(
            user_id,
            kind,
            title=shipped.project.title or request[:60],
            location=location,
            meta={
                "prompt": request,
                "kind": shipped.project.kind,
                "qa_rounds": len(shipped.rounds),
                "qa_ok": shipped.ok,
                "files": [str(p.relative_to(shipped.project.root)) for p in shipped.project.files][:20],
            },
        )
        text = shipped.summary()
        if shipped.deployment is None:
            text += f"\n\nBelum ada kredensial deploy, jadi aku kasih bundle lokal:\n`{shipped.bundle}`"
        screenshots = []
        if shipped.final_report:
            screenshots = shipped.final_report.screenshots()[:2]
        return Outcome(
            text=text,
            url=location,
            files=[shipped.bundle] if shipped.bundle else [],
            milestone_screenshots=screenshots,
            skipped_qa=not self.qa and False,
            tokens=estimate_tokens_for(Intent.WEB_STATIC),
        )

    # ------------------------------------------------------------------ n8n
    async def _do_n8n(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        request = _clean_prompt(route.args.get("text") or "")
        if progress:
            await progress(25, "Merancang workflow")
        plan = await self.n8n.plan(request)
        path = self.n8n.save(plan)
        text = (
            f"Workflow *{plan.name}* siap.\n"
            f"Node: {len(plan.nodes)} | trigger: {plan.trigger_kind}\n"
            f"File: `{path}`"
        )
        if plan.issues:
            text += "\nCatatan: " + "; ".join(plan.issues[:3])

        try:
            if progress:
                await progress(70, "Import ke n8n")
            imported = await self.n8n.import_workflow(plan)
            text += f"\n\nSudah diimport: {imported['url']} (aktif: {imported['active']})"
            location = imported["url"]
        except MissingSecretError:
            # n8n itself is running (it is started for you), it just has no API
            # key yet, and only the owner can create one. Say exactly where to
            # click instead of leaving the user with a JSON file and a shrug.
            base = self.settings.n8n_base_url.rstrip("/")
            text += (
                "\n\nn8n sudah jalan, tapi aku belum bisa import otomatis "
                "karena belum ada API key.\n"
                f"Bikin sekali saja di sini: {base} → Settings → n8n API → Create API key, "
                "lalu isi `N8N_API_KEY` di .env.\n"
                "Sementara ini JSON workflow-nya aku lampirkan, tinggal di-import manual."
            )
            location = str(path)
        except Exception as exc:  # noqa: BLE001
            text += f"\n\nImport otomatis gagal: {str(exc)[:150]}"
            location = str(path)

        await self.memory.register_artifact(
            user_id,
            KIND_N8N,
            title=plan.name,
            location=location,
            meta={"prompt": request, "nodes": len(plan.nodes), "trigger": plan.trigger_kind},
        )
        return Outcome(text=text, files=[str(path)], tokens=3000)

    # ---------------------------------------------------------------- learn
    async def _do_learn(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        query = _strip_learn_prefix(_clean_prompt(route.args.get("text") or ""))
        if not query:
            return Outcome(text="Mau belajar tentang apa? Contoh: /belajar retrieval augmented generation")
        result = await learn_topic(
            query,
            search=self.search,
            gateway=self.gateway,
            memory=self.memory,
            user_id=user_id,
            progress=progress,
        )
        await self.memory.register_artifact(
            user_id,
            KIND_RESEARCH,
            title=query[:80],
            location=result["note_path"],
            meta={"sources": result["sources"][:5], "providers": result["providers"]},
        )
        text = (
            f"📚 *{query}*\n\n{result['summary']}\n\n"
            f"Sumber: {len(result['sources'])} ({', '.join(result['providers'])})\n"
            f"Catatan: `{result['note_path']}`"
        )
        return Outcome(text=text, files=[result["note_path"]], tokens=6000)

    # ----------------------------------------------------------------- link
    async def _do_link_ingest(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        urls = route.args.get("urls") or []
        question = str(route.args.get("question") or "").strip()
        if not urls:
            return Outcome(text="Kirim link-nya ya (YouTube, TikTok, IG, atau artikel).")

        url = urls[0]
        content = await self.links.ingest(url, progress=progress)

        if not content.ok:
            reason = content.metadata.get("blocked_reason", "")
            text = (
                f"Aku tidak bisa membaca isi link itu.\n"
                f"Alasan: {reason[:200] or 'platform memblokir akses'}\n\n"
                "Pilihanmu:\n"
                "1. Kirim file videonya langsung ke sini (aku transkrip sendiri).\n"
                "2. Set `YTDLP_COOKIES_FILE` biar aku pakai cookie browser kamu.\n"
                "3. Set token YouTube dan aku upload ke channel kamu sebagai video privat, "
                "lalu aku baca transkripnya."
            )
            return Outcome(text=text)

        if progress:
            await progress(80, "Menjawab pertanyaan tentang isi link")
        answer = await answer_about_link(content, question, self.gateway, user_id=user_id)
        await self.memory.register_artifact(
            user_id,
            KIND_LINK,
            title=content.title[:80] or url,
            location=url,
            meta={
                "method": content.method,
                "platform": content.metadata.get("platform"),
                "duration": content.duration,
                "transcript_chars": len(content.transcript),
            },
        )
        header = f"🎬 *{content.title}*" if content.title else "🔗 Link"
        return Outcome(
            text=f"{header}\n({content.method})\n\n{answer}",
            tokens=8000,
        )

    # ------------------------------------------------------------- reminder
    async def _do_reminder(self, user_id: int, route: Route, *, progress=None, chat_id=0, **kwargs) -> Outcome:
        text = str(route.args.get("text") or "")
        parsed = parse_reminder(text, tz=self.settings.tz)
        if self.scheduler is None:
            return Outcome(text="Scheduler sedang mati, jadi aku belum bisa bikin pengingat.")
        row = await self.scheduler.schedule(
            user_id=user_id,
            chat_id=chat_id or user_id,
            text=parsed.text,
            when=parsed.when,
            repeat=parsed.repeat,
        )
        repeat_note = f" (berulang: {parsed.repeat})" if parsed.repeat else ""
        return Outcome(
            text=f"⏰ Pengingat diset: *{parsed.text}*\nWaktu: {parsed.human}{repeat_note}\nID: {row['id']}",
            tokens=100,
        )

    # -------------------------------------------------------- memory/feedback
    async def _do_memory_query(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        mode = route.args.get("mode", "stats")
        text = str(route.args.get("text") or "")
        if mode == "write":
            fact = _strip_prefix(text, ["ingat", "remember", "catat"])
            if fact:
                await self.memory.remember_fact(user_id, fact)
                return Outcome(text=f"Oke, aku ingat: {fact[:200]}")
            return Outcome(text="Mau aku ingat apa?")
        if mode == "forget":
            await self.memory.vectors.delete_user(user_id)
            return Outcome(text="Memori vektor kamu sudah aku bersihkan.")
        stats = await self.memory.stats(user_id)
        budget_text = await self.budget.summary(user_id) if self.budget else ""
        cache_stats = await self.cache.stats(user_id) if self.cache else {"entries": 0, "hits": 0}
        errors = await self.store.recent_errors(3)
        text_out = (
            f"📊 *Status memori*\n"
            f"Pesan tersimpan: {stats['messages']}\n"
            f"Vektor memori: {stats['vectors']} ({stats['backend']})\n"
            f"Artifact: {stats['artifacts']}\n"
            f"Cache: {cache_stats['entries']} entri, {cache_stats['hits']} hit\n\n"
            f"{budget_text}"
        )
        if errors:
            text_out += "\n\nError terakhir:\n" + "\n".join(
                f"- [{e['module']}] {e['message'][:70]}" for e in errors
            )
        return Outcome(text=text_out, tokens=50)

    async def _do_feedback(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        text = str(route.args.get("text") or "")
        body = _strip_prefix(text, ["feedback", "saran", "kritik"])
        if body:
            await self.store.add_feedback(user_id, body)
            await self.memory.remember_fact(user_id, f"Feedback user: {body}", kind="feedback")
            return Outcome(text="Catatanmu aku simpan. Makasih!")
        return Outcome(text="Tulis feedbacknya setelah kata 'feedback', ya.")

    async def _do_help(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        return Outcome(text=HELP_TEXT, tokens=0)

    async def _do_unknown(self, user_id: int, route: Route, *, progress=None, **kwargs) -> Outcome:
        return await self._do_chat(user_id, route, progress=progress, **kwargs)


HELP_TEXT = """*Yang bisa aku lakukan*

Chat & ingatan
- Ngobrol biasa, aku ingat semua obrolan sebelumnya.
- "perbaiki yang tadi" / "lanjutkan itu" / "yang kemarin" - aku resolve ke pekerjaan yang benar.
- /pikir <pertanyaan> - analisis 5 pass bertahap.

Media
- "buatkan gambar ..." lalu "edit gambarnya jadi lebih terang".
- "bikin video ..." lalu "potong jadi 10 detik, tambah subtitle".
- "bacakan ini jadi suara" / kirim voice note untuk transkrip.

Web
- "buat website portofolio" - static, dites Playwright dulu, baru dikirim.
- "buat web app login + database" - FastAPI + Supabase.

Otomasi
- "buatkan workflow n8n untuk ..." - langsung diimport kalau API key ada.
- /ingatkan <waktu> <hal> - reminder, bisa berulang.

Belajar & link
- /belajar <topik> - cari di internet, rangkum, simpan permanen.
- Kirim link YouTube/TikTok/IG/artikel - aku baca isinya dan jawab pertanyaanmu.

Lain
- /memori - statistik memori + token terpakai.
- /feedback <pesan> - catat saranmu.
- /batal - batalkan konfirmasi yang sedang menunggu."""


# ------------------------------------------------------------------ helpers
def _kind_from_question(text: str) -> set[str]:
    """Map the noun in a status question to artifact kinds."""
    from app.memory.artifacts import KIND_AUDIO, KIND_IMAGE, KIND_N8N, KIND_VIDEO, KIND_WEB, KIND_WEB_APP

    lowered = text.lower()
    kinds: set[str] = set()
    if "web" in lowered or "situs" in lowered or "website" in lowered:
        kinds |= {KIND_WEB, KIND_WEB_APP}
    if "gambar" in lowered or "foto" in lowered or "logo" in lowered:
        kinds.add(KIND_IMAGE)
    if "video" in lowered:
        kinds.add(KIND_VIDEO)
    if "audio" in lowered or "suara" in lowered or "voice" in lowered:
        kinds.add(KIND_AUDIO)
    if "n8n" in lowered or "workflow" in lowered:
        kinds.add(KIND_N8N)
    return kinds


def _clean_prompt(text: str) -> str:
    text = re.sub(r"^\s*/\w+\s*", "", text or "")
    return text.strip()


def _strip_prefix(text: str, prefixes: list[str]) -> str:
    for prefix in prefixes:
        text = re.sub(rf"^\s*/?{prefix}\b[:\s]*", "", text, flags=re.I)
    return text.strip()


def _strip_learn_prefix(text: str) -> str:
    return _strip_prefix(text, ["belajar", "pelajari", "riset", "research", "search", "cari tahu"])


def _extract_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>\"']+", text or "")
    return match.group(0).rstrip(".,;") if match else ""


def _strip_url(text: str) -> str:
    return re.sub(r"https?://[^\s<>\"']+", " ", text or "").strip()


def _size_pair(size: str) -> tuple[int, int] | None:
    match = re.match(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", size or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _extract_size(text: str) -> str:
    match = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", text)
    if match:
        return f"{match.group(1)}x{match.group(2)}"
    if "potrait" in text.lower() or "vertikal" in text.lower():
        return "768x1024"
    if "landscape" in text.lower() or "horizontal" in text.lower():
        return "1024x768"
    return ""


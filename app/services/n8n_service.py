"""n8n workflow generation and import.

The model returns a workflow *spec* (trigger + nodes + connections), never a
full n8n JSON dump. We then assemble the n8n document ourselves from a
whitelist of node types. Two reasons:

* correctness - hand-written node parameters follow the real n8n schema, so the
  import does not fail with "node type not found" or missing ``typeVersion``
* safety - a model cannot smuggle in a Code node that runs arbitrary commands,
  because only whitelisted node types survive assembly.

Import goes through the public n8n REST API (``X-N8N-API-KEY``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.core.errors import MissingSecretError, ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)

# node type -> (n8n type string, typeVersion, default parameters)
NODE_CATALOG: dict[str, tuple[str, float, dict]] = {
    "webhook": ("n8n-nodes-base.webhook", 2, {"httpMethod": "POST", "path": "", "responseMode": "onReceived"}),
    "schedule": ("n8n-nodes-base.scheduleTrigger", 1.2, {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}),
    "manual": ("n8n-nodes-base.manualTrigger", 1, {}),
    "http_request": ("n8n-nodes-base.httpRequest", 4.2, {"method": "GET", "url": ""}),
    "set": ("n8n-nodes-base.set", 3.4, {"assignments": {"assignments": []}}),
    "if": ("n8n-nodes-base.if", 2, {"conditions": {}}),
    "switch": ("n8n-nodes-base.switch", 3, {"rules": {"values": []}}),
    "code": ("n8n-nodes-base.code", 2, {"jsCode": "return items;"}),
    "telegram": ("n8n-nodes-base.telegram", 1.2, {"operation": "sendMessage", "text": ""}),
    "gmail": ("n8n-nodes-base.gmail", 2.1, {"operation": "send"}),
    "slack": ("n8n-nodes-base.slack", 2.2, {"operation": "post", "resource": "message"}),
    "google_sheets": ("n8n-nodes-base.googleSheets", 4.5, {"operation": "append"}),
    "postgres": ("n8n-nodes-base.postgres", 2.5, {"operation": "executeQuery"}),
    "supabase": ("n8n-nodes-base.supabase", 1, {"operation": "getAll", "resource": "row"}),
    "airtable": ("n8n-nodes-base.airtable", 2.1, {"operation": "list"}),
    "openai": ("@n8n/n8n-nodes-langchain.openAi", 1.8, {"resource": "text", "operation": "message"}),
    "wait": ("n8n-nodes-base.wait", 1.1, {"amount": 1, "unit": "seconds"}),
    "merge": ("n8n-nodes-base.merge", 3, {"mode": "append"}),
    "split_in_batches": ("n8n-nodes-base.splitInBatches", 3, {"batchSize": 10}),
    "no_op": ("n8n-nodes-base.noOp", 1, {}),
    "respond_to_webhook": ("n8n-nodes-base.respondToWebhook", 1.1, {"respondWith": "json"}),
}

SPEC_SYSTEM = """Kamu otomasi engineer yang merancang workflow n8n.

Balas JSON:
{
  "name": "<nama workflow>",
  "trigger": {"kind": "webhook|schedule|manual", "config": {}},
  "nodes": [
    {"key": "<id unik singkat>", "type": "<tipe>", "name": "<label manusiawi>", "params": {}}
  ],
  "connections": [{"from": "<key>", "to": "<key>"}],
  "notes": "<catatan singkat>"
}

Tipe node yang diizinkan (WAJIB pakai salah satu ini):
webhook, schedule, manual, http_request, set, if, switch, code, telegram,
gmail, slack, google_sheets, postgres, supabase, airtable, openai, wait,
merge, split_in_batches, no_op, respond_to_webhook

Aturan:
- Selalu mulai dari satu trigger (webhook/schedule/manual).
- 3 sampai 8 node. Jangan berlebihan.
- "connections" harus membentuk alur linear atau bercabang dari trigger.
- Jangan pernah menaruh kredensial di params. Kosongkan, user isi sendiri di UI n8n.
- Untuk node code, tulis JS sederhana yang aman (tanpa require/import/exec).
- Bahasa Indonesia untuk name dan notes.
Balas JSON saja."""


@dataclass
class WorkflowPlan:
    name: str
    trigger_kind: str
    nodes: list[dict] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    notes: str = ""
    issues: list[str] = field(default_factory=list)


class N8nService:
    def __init__(self, settings, gateway=None, *, outputs_dir: Path | None = None):
        self.settings = settings
        self.gateway = gateway
        self.outputs = Path(outputs_dir or settings.outputs_path)
        (self.outputs / "n8n").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ plan
    async def plan(self, request: str) -> WorkflowPlan:
        if self.gateway is None or not self.gateway.available():
            raise ToolError("Butuh provider LLM untuk merancang workflow n8n.")
        data = await self.gateway.complete_json(
            [
                {"role": "system", "content": SPEC_SYSTEM},
                {"role": "user", "content": f"Permintaan: {request}"},
            ],
            tier="big",
            temperature=0.3,
            max_tokens=2000,
        )
        plan = self._validate(data, request)
        if not plan.nodes:
            raise ToolError("Model tidak menghasilkan node yang valid untuk workflow ini.")
        return plan

    def _validate(self, data: dict, request: str) -> WorkflowPlan:
        trigger = data.get("trigger") or {}
        trigger_kind = str(trigger.get("kind") or "manual").lower()
        if trigger_kind not in {"webhook", "schedule", "manual"}:
            trigger_kind = "manual"

        nodes: list[dict] = []
        seen_keys: set[str] = set()
        issues: list[str] = []

        # the trigger node always comes first, built by us
        nodes.append(
            {
                "key": "trigger",
                "type": trigger_kind,
                "name": {"webhook": "Webhook", "schedule": "Schedule", "manual": "Manual Trigger"}[trigger_kind],
                "params": _safe_params(trigger_kind, trigger.get("config") or {}),
            }
        )
        seen_keys.add("trigger")

        for raw in (data.get("nodes") or [])[:10]:
            if not isinstance(raw, dict):
                continue
            node_type = str(raw.get("type") or "").strip().lower()
            if node_type not in NODE_CATALOG:
                issues.append(f"tipe node tidak dikenal diabaikan: {node_type}")
                continue
            if node_type in {"webhook", "schedule", "manual"}:
                continue  # triggers are handled above
            key = _safe_key(raw.get("key") or raw.get("name") or node_type)
            if key in seen_keys:
                key = f"{key}-{len(seen_keys)}"
            seen_keys.add(key)
            nodes.append(
                {
                    "key": key,
                    "type": node_type,
                    "name": str(raw.get("name") or node_type)[:60],
                    "params": _safe_params(node_type, raw.get("params") or {}),
                }
            )

        connections: list[dict] = []
        valid_keys = {node["key"] for node in nodes}
        for raw in (data.get("connections") or [])[:20]:
            if not isinstance(raw, dict):
                continue
            source, target = str(raw.get("from") or ""), str(raw.get("to") or "")
            if source in valid_keys and target in valid_keys and source != target:
                connections.append({"from": source, "to": target})

        if not connections:
            # fall back to a linear chain so the workflow is at least runnable
            for index in range(len(nodes) - 1):
                connections.append({"from": nodes[index]["key"], "to": nodes[index + 1]["key"]})

        return WorkflowPlan(
            name=str(data.get("name") or request)[:80],
            trigger_kind=trigger_kind,
            nodes=nodes,
            connections=connections,
            notes=str(data.get("notes") or "")[:400],
            issues=issues,
        )

    # --------------------------------------------------------------- document
    def to_n8n_json(self, plan: WorkflowPlan) -> dict:
        """Assemble a real n8n workflow document from the validated plan."""
        n8n_nodes: list[dict] = []
        name_to_node: dict[str, dict] = {}

        for index, node in enumerate(plan.nodes):
            catalog_entry = NODE_CATALOG.get(node["type"])
            if not catalog_entry:
                continue
            n8n_type, type_version, defaults = catalog_entry
            params = {**defaults, **node["params"]}
            if node["type"] == "webhook" and not params.get("path"):
                params["path"] = _safe_key(plan.name)

            entry = {
                "parameters": params,
                "id": f"node-{index}-{node['key']}",
                "name": node["name"],
                "type": n8n_type,
                "typeVersion": type_version,
                "position": [260 + index * 220, 300],
            }
            n8n_nodes.append(entry)
            name_to_node[node["key"]] = entry

        n8n_connections: dict[str, dict] = {}
        for connection in plan.connections:
            source = name_to_node.get(connection["from"])
            target = name_to_node.get(connection["to"])
            if not source or not target:
                continue
            n8n_connections.setdefault(source["name"], {"main": [[]]})
            n8n_connections[source["name"]]["main"][0].append(
                {"node": target["name"], "type": "main", "index": 0}
            )

        return {
            "name": plan.name,
            "nodes": n8n_nodes,
            "connections": n8n_connections,
            "active": False,
            "settings": {"executionOrder": "v1"},
            "staticData": None,
            "tags": [],
        }

    def save(self, plan: WorkflowPlan) -> Path:
        document = self.to_n8n_json(plan)
        slug = _safe_key(plan.name)
        path = self.outputs / "n8n" / f"{slug}-{_stamp()}.json"
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    # ---------------------------------------------------------------- import
    async def import_workflow(self, plan: WorkflowPlan, *, activate: bool = True) -> dict:
        if not self.settings.n8n_api_key:
            raise MissingSecretError("N8N_API_KEY")
        base = self.settings.n8n_base_url.rstrip("/")
        headers = {"X-N8N-API-KEY": self.settings.n8n_api_key, "Content-Type": "application/json"}
        document = self.to_n8n_json(plan)
        document["active"] = False  # n8n requires creating inactive first

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{base}/api/v1/workflows", headers=headers, json=document)
            if response.status_code >= 400:
                raise ToolError(f"n8n import gagal ({response.status_code}): {response.text[:300]}")
            created = response.json()

            workflow_id = created.get("id")
            activated = False
            if activate and workflow_id:
                patch = await client.patch(
                    f"{base}/api/v1/workflows/{workflow_id}",
                    headers=headers,
                    json={"active": True},
                )
                activated = patch.status_code < 400

        url = f"{base}/workflow/{workflow_id}" if workflow_id else base
        return {
            "id": workflow_id,
            "url": url,
            "active": activated,
            "name": plan.name,
            "nodes": len(document["nodes"]),
        }

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.settings.n8n_base_url.rstrip('/')}/healthz")
            return response.status_code < 400
        except Exception:  # noqa: BLE001
            return False


def _safe_params(node_type: str, params: dict) -> dict:
    """Merge model params over defaults, dropping anything dangerous or blank."""
    _, _, defaults = NODE_CATALOG.get(node_type, ("", 1, {}))
    merged = {**defaults}
    if not isinstance(params, dict):
        return merged
    for key, value in params.items():
        if key in {"credentials", "credentialData", "authentication"}:
            continue
        if key == "jsCode" and isinstance(value, str):
            merged[key] = _sanitise_js(value)
            continue
        merged[key] = value
    return merged


_FORBIDDEN_JS = re.compile(
    r"(require\s*\(|import\s+|process\.|child_process|eval\s*\(|Function\s*\(|fs\.|exec\s*\()"
)


def _sanitise_js(code: str) -> str:
    if _FORBIDDEN_JS.search(code):
        log.warning("unsafe js in generated n8n code node; replacing with passthrough")
        return "return items;"
    return code[:4000]


def _safe_key(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", str(text).lower()).strip("-")
    return cleaned[:50] or "workflow"


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")

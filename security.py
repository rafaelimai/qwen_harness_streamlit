"""Guardas determinísticos e semânticos para conteúdo e execuções da harness."""
from __future__ import annotations

import ast
import base64
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4


POLICY_VERSION = "security-v1"
RISK_ORDER = {"low": 0, "moderate": 1, "high": 2, "critical": 3}


@dataclass
class Finding:
    """Achado explicável associado a uma linha e a um efeito potencial."""

    rule: str
    severity: str
    message: str
    line: int | None = None
    evidence: str | None = None


@dataclass
class Assessment:
    """Resultado serializável de uma avaliação de segurança."""

    risk: str = "low"
    findings: list[Finding] = field(default_factory=list)
    detected_intents: list[str] = field(default_factory=list)
    semantic_review: dict | None = None

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)
        if RISK_ORDER[finding.severity] > RISK_ORDER[self.risk]:
            self.risk = finding.severity

    def to_dict(self) -> dict:
        return {
            "risk": self.risk,
            "findings": [asdict(item) for item in self.findings],
            "detected_intents": sorted(set(self.detected_intents)),
            "semantic_review": self.semantic_review,
            "policy_version": POLICY_VERSION,
        }


INJECTION_PATTERNS = (
    (r"\b(ignore|disregard|forget)\b.{0,80}\b(previous|prior|above|instructions?)\b",
     "override_instructions", "high", "Tenta substituir instruções anteriores."),
    (r"\b(ignorar|desconsiderar|esquecer)\b.{0,80}\b(instruções?|regras?|anteriores?)\b",
     "override_instructions", "high", "Tenta substituir instruções anteriores."),
    (r"\b(system prompt|developer message|prompt do sistema|mensagem do desenvolvedor)\b",
     "access_control_instructions", "moderate", "Menciona instruções reservadas de controle."),
    (r"\b(exfiltrat|send|upload|envie|publique)\b.{0,100}\b(secret|token|password|credential|segredo|senha)\b",
     "exfiltrate_data", "critical", "Solicita transferência de credenciais ou segredos."),
    (r"\b(disable|bypass|desative|contorne)\b.{0,80}\b(safety|security|policy|segurança|política)\b",
     "disable_safeguards", "high", "Tenta desativar ou contornar controles de segurança."),
)


def assess_content(text: str, source: str = "unknown", can_authorize_actions: bool = False) -> dict:
    """Classifica instruções suspeitas sem executar ou obedecer ao conteúdo."""
    assessment = Assessment()
    sample = text[:200_000]
    for pattern, intent, severity, message in INJECTION_PATTERNS:
        match = re.search(pattern, sample, flags=re.IGNORECASE | re.DOTALL)
        if match:
            assessment.detected_intents.append(intent)
            assessment.add(Finding("content." + intent, severity, message,
                                   evidence=match.group(0)[:240]))
    result = assessment.to_dict()
    result.update({
        "source": source,
        "trust": "authenticated" if source == "user" else "untrusted",
        "can_authorize_actions": bool(can_authorize_actions),
        "content_sha256": sha256(text.encode("utf-8", errors="replace")).hexdigest(),
    })
    return result


CALL_RISKS = {
    "os.system": ("critical", "executa uma linha de comando por meio do shell"),
    "os.popen": ("critical", "abre um comando do sistema e captura sua saída"),
    "subprocess.run": ("high", "inicia um processo filho; argumentos e shell=True precisam de revisão"),
    "subprocess.Popen": ("high", "inicia um processo filho com controle de streams e ambiente"),
    "subprocess.call": ("high", "inicia um processo filho"),
    "subprocess.check_call": ("high", "inicia um processo filho"),
    "subprocess.check_output": ("high", "inicia um processo filho e captura sua saída"),
    "socket.create_connection": ("high", "tenta abrir uma conexão de rede"),
    "requests.get": ("high", "tenta baixar conteúdo da rede"),
    "requests.post": ("critical", "tenta enviar dados pela rede"),
    "urllib.request.urlopen": ("high", "tenta acessar um recurso de rede"),
    "ctypes.CDLL": ("critical", "carrega uma biblioteca nativa fora das garantias usuais do Python"),
    "ctypes.PyDLL": ("critical", "carrega uma biblioteca nativa com acesso ao runtime Python"),
    "importlib.import_module": ("high", "carrega um módulo determinado dinamicamente"),
    "pickle.loads": ("critical", "desserializa bytes que podem executar código se forem maliciosos"),
    "marshal.loads": ("critical", "reconstrói objetos de bytecode, padrão comum em payloads ofuscados"),
    "builtins.eval": ("critical", "avalia texto como expressão Python"),
    "builtins.exec": ("critical", "executa texto ou bytecode como Python"),
    "builtins.compile": ("high", "compila texto dinâmico para posterior execução"),
    "builtins.__import__": ("high", "importa módulo dinamicamente"),
}

SENSITIVE_IMPORTS = {
    "subprocess": "permite iniciar outros programas",
    "socket": "permite criar conexões de rede",
    "requests": "permite realizar requisições HTTP",
    "urllib": "pode acessar recursos de rede",
    "ctypes": "permite chamar código nativo",
    "pickle": "pode executar código ao desserializar dados não confiáveis",
    "marshal": "pode reconstruir bytecode e ocultar comportamento",
    "importlib": "permite imports dinâmicos",
    "base64": "pode ocultar strings ou payloads; o uso de decodificação é inspecionado",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return "builtins." + node.id if node.id in {"eval", "exec", "compile", "__import__"} else node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        if parent.startswith("builtins."):
            parent = parent.removeprefix("builtins.")
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _source_segment(source: str, node: ast.AST) -> str:
    return (ast.get_source_segment(source, node) or "").strip()[:300]


def _decoded_payload(value: str | bytes) -> str | None:
    try:
        raw = value.encode("ascii") if isinstance(value, str) else value
        if len(raw) < 16 or len(raw) > 100_000:
            return None
        decoded = base64.b64decode(raw, validate=True)
        return decoded[:100_000].decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return None


def assess_python(source: str) -> dict:
    """Analisa AST, bibliotecas, ofuscação e payloads Base64 de um script Python."""
    assessment = Assessment()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        assessment.add(Finding("python.syntax", "high", "O script não pôde ser analisado com segurança.",
                               exc.lineno, str(exc)))
        return assessment.to_dict()

    imported: dict[str, tuple[str, int]] = {}
    used_calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                imported[item.asname or item.name.split(".")[0]] = (item.name, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                imported[item.asname or item.name] = (f"{node.module}.{item.name}", node.lineno)

    def canonical(name: str) -> str:
        first, dot, rest = name.partition(".")
        target = imported.get(first, (first, 0))[0]
        return target + (dot + rest if dot else "")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = canonical(_call_name(node.func))
        used_calls.add(name)
        risk = CALL_RISKS.get(name)
        if risk:
            severity, effect = risk
            message = f"`{name}` {effect}."
            if name.startswith("subprocess."):
                shell_true = any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                                 for k in node.keywords)
                if shell_true:
                    severity = "critical"
                    message += " Foi usado `shell=True`, permitindo interpretação de metacaracteres pelo shell."
            assessment.add(Finding("python.dangerous_call", severity, message, node.lineno,
                                   _source_segment(source, node)))

        lower_name = name.lower()
        if lower_name.endswith(("b64decode", "urlsafe_b64decode", "decodebytes")):
            assessment.add(Finding(
                "python.base64_decode", "moderate",
                f"`{name}` decodifica Base64; isso pode ser legítimo, mas também ocultar código ou comandos.",
                node.lineno, _source_segment(source, node)))
            parent_calls = [p for p in ast.walk(tree) if isinstance(p, ast.Call)
                            and any(child is node for child in ast.walk(p) if child is not p)]
            dangerous_parent = next((_call_name(p.func) for p in parent_calls
                                     if canonical(_call_name(p.func)) in {
                                         "builtins.exec", "builtins.eval", "builtins.compile",
                                         "marshal.loads", "pickle.loads"}), None)
            if dangerous_parent:
                assessment.add(Finding(
                    "python.encoded_execution", "critical",
                    f"O resultado de Base64 alimenta `{dangerous_parent}`, indicando execução de payload ofuscado.",
                    node.lineno, _source_segment(source, next(p for p in parent_calls
                                                               if _call_name(p.func) == dangerous_parent))))

    # Strings Base64 literais são decodificadas apenas durante a análise, com limite estrito.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, (str, bytes)):
            continue
        decoded = _decoded_payload(node.value)
        if decoded is None:
            continue
        suspicious = re.search(
            r"\b(exec|eval|__import__|subprocess|os\.system|socket|requests\.(get|post)|/bin/(ba)?sh)\b",
            decoded, flags=re.IGNORECASE)
        if suspicious:
            assessment.add(Finding(
                "python.encoded_payload", "critical",
                "Uma constante Base64 decodifica para conteúdo com sinais de execução, processo ou rede.",
                node.lineno, decoded[:240]))

    # Imports sensíveis sem chamada conhecida ainda merecem alerta contextual, não bloqueio automático.
    for alias, (module, line) in imported.items():
        root = module.split(".")[0]
        if root not in SENSITIVE_IMPORTS:
            continue
        used = sorted(call for call in used_calls if call == module or call.startswith(module + "."))
        if used:
            detail = "Uso observado: " + ", ".join(f"`{item}`" for item in used[:6]) + "."
        else:
            detail = "Nenhuma chamada de risco conhecida foi observada; o import isolado não implica abuso."
        assessment.add(Finding(
            "python.sensitive_library", "moderate",
            f"A biblioteca `{root}` {SENSITIVE_IMPORTS[root]}. {detail}", line, f"import {module}"))

    # Combinações recorrentes de empacotamento/ofuscação.
    roots = {module.split(".")[0] for module, _ in imported.values()}
    if "base64" in roots and ({"marshal", "zlib", "importlib"} & roots):
        assessment.add(Finding(
            "python.obfuscation_chain", "high",
            "A combinação de Base64 com marshal, zlib ou importlib pode reconstruir e executar um payload oculto.",
            evidence=", ".join(sorted(roots & {"base64", "marshal", "zlib", "importlib"}))))

    return assessment.to_dict()


TEXT_EXTENSIONS = {'.csv', '.json', '.txt', '.md', '.py', '.sql', '.yaml', '.yml', '.toml', '.log'}


def file_sha256(path: Path) -> str:
    """Calcula hash em streaming para não carregar artefatos grandes na memória."""
    digest = sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def assess_file(path: Path, source: str = "file") -> dict:
    """Classifica um arquivo aceito antes de permitir sua visualização."""
    path = path.resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("A avaliação exige um arquivo regular.")
    if path.stat().st_size > 20 * 1024 * 1024:
        assessment = Assessment()
        assessment.add(Finding("file.size", "moderate",
                               "O arquivo excede 20 MiB e não foi inspecionado integralmente."))
        return assessment.to_dict() | {"source": source, "content_sha256": file_sha256(path)}
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        try:
            import pymupdf
            with pymupdf.open(path) as document:
                text = '\n'.join(page.get_text() for page in document)[:200_000]
            result = assess_content(text, source, False)
            if not text.strip():
                result["risk"] = "moderate"
                result["findings"].append(asdict(Finding(
                    "file.uninspected_pdf", "moderate",
                    "O PDF não possui texto extraível; imagens ou OCR não foram inspecionados.")))
            return result
        except Exception as exc:
            assessment = Assessment()
            assessment.add(Finding("file.pdf_parse", "moderate",
                                   "O PDF não pôde ser inspecionado com segurança.", evidence=str(exc)[:240]))
            return assessment.to_dict() | {"source": source,
                                            "content_sha256": file_sha256(path)}
    if suffix in TEXT_EXTENSIONS:
        try:
            text = path.read_text(encoding="utf-8")[:200_000]
        except UnicodeDecodeError:
            assessment = Assessment()
            assessment.add(Finding("file.encoding", "moderate",
                                   "O arquivo textual não é UTF-8 e não foi inspecionado integralmente."))
            return assessment.to_dict() | {"source": source,
                                            "content_sha256": file_sha256(path)}
        result = assess_python(text) if suffix == '.py' else assess_content(text, source, False)
        result.setdefault("source", source)
        result.setdefault("content_sha256", sha256(text.encode("utf-8")).hexdigest())
        return result
    assessment = Assessment()
    assessment.add(Finding("file.unsupported", "moderate",
                           "O formato não possui um analisador de segurança e não será exibido."))
    return assessment.to_dict() | {"source": source,
                                    "content_sha256": file_sha256(path)}


def safe_to_display(assessment: dict) -> bool:
    """Permite visualização somente quando nenhum risco foi encontrado."""
    return assessment.get("risk") == "low"


def merge_semantic_review(assessment: dict, review: dict | None) -> dict:
    """Anexa revisão semântica e eleva risco; nunca reduz achados determinísticos."""
    assessment = json.loads(json.dumps(assessment))
    assessment["semantic_review"] = review
    if not review:
        return assessment
    semantic_risk = review.get("risk")
    if semantic_risk in RISK_ORDER and RISK_ORDER[semantic_risk] > RISK_ORDER[assessment["risk"]]:
        assessment["risk"] = semantic_risk
    if review.get("aligned_with_user_goal") is False and RISK_ORDER[assessment["risk"]] < RISK_ORDER["high"]:
        assessment["risk"] = "high"
    return assessment


def decision_for(assessment: dict, external_origin: bool = False) -> str:
    """Traduz risco em política determinística de execução."""
    if assessment["risk"] == "critical":
        return "deny"
    if assessment["risk"] in {"moderate", "high"} or external_origin:
        return "require_approval"
    return "allow"


class SecurityStore:
    """Persiste decisões auditáveis e aprovações vinculadas ao conteúdo."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / "security"
        self.directory.mkdir(exist_ok=True)

    def create(self, *, script: str, script_sha256: str, arguments: list[str], timeout: int,
               purpose: str, assessment: dict, decision: str) -> dict:
        identifier = "sec_" + uuid4().hex
        record = {
            "id": identifier,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_version": POLICY_VERSION,
            "script": script,
            "script_sha256": script_sha256,
            "arguments": arguments,
            "timeout": timeout,
            "purpose": purpose,
            "assessment": assessment,
            "decision": decision,
            "approved": decision == "allow",
            "rejected": decision == "deny",
        }
        self._write(record)
        return record

    def _path(self, identifier: str) -> Path:
        if not re.fullmatch(r"sec_[0-9a-f]{32}", identifier):
            raise ValueError("Identificador de segurança inválido.")
        return self.directory / f"{identifier}.json"

    def _write(self, record: dict) -> None:
        path = self._path(record["id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def get(self, identifier: str) -> dict:
        path = self._path(identifier)
        if not path.is_file():
            raise ValueError("Decisão de segurança não encontrada nesta conversa.")
        return json.loads(path.read_text(encoding="utf-8"))

    def resolve(self, identifier: str, approved: bool) -> dict:
        record = self.get(identifier)
        if record["decision"] != "require_approval" or record.get("approved") or record.get("rejected"):
            raise ValueError("Esta decisão não está aguardando aprovação.")
        record["approved"] = bool(approved)
        record["rejected"] = not approved
        record["resolved_at"] = datetime.now(timezone.utc).isoformat()
        self._write(record)
        return record

    def validate(self, identifier: str, *, script: str, script_sha256: str,
                 arguments: list[str], timeout: int) -> dict:
        record = self.get(identifier)
        expected = (record["script"], record["script_sha256"], record["arguments"], record["timeout"])
        actual = (script, script_sha256, arguments, timeout)
        if expected != actual:
            raise ValueError("O script ou seus parâmetros mudaram depois da avaliação; solicite nova análise.")
        if record.get("rejected"):
            raise ValueError("A execução foi rejeitada pelo usuário ou pela política.")
        if not record.get("approved"):
            raise ValueError("A execução ainda aguarda aprovação humana.")
        return record


def format_alerts(assessment: dict) -> list[str]:
    """Produz alertas curtos para a interface humana."""
    alerts = []
    for item in assessment.get("findings", []):
        location = f" (linha {item['line']})" if item.get("line") else ""
        alerts.append(f"[{item['severity'].upper()}] {item['message']}{location}")
    review = assessment.get("semantic_review") or {}
    for behavior in review.get("unexpected_behaviors", [])[:6]:
        alerts.append(f"[SEMÂNTICO] {behavior}")
    return alerts

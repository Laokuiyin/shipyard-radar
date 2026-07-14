from __future__ import annotations

import re
from datetime import date
from typing import Any

import httpx

from shipwatch.config import Settings
from shipwatch.domain import Extraction, Milestone
from shipwatch.parsers import extract_json_object
from shipwatch.text import parse_date


START_PATTERN = re.compile(r"点火开工|正式开工|开工仪式|开板|首块钢板(?:切割|开切)|钢板切割|开工")
HISTORICAL_START_PATTERN = re.compile(r"(?:已|已经|此前|曾|早在|自[^。；]{0,20}以来)[^。；]{0,16}开工")
CURRENT_EVENT_PATTERN = re.compile(r"今日|今天|当日|近日|日前|正式|举行|仪式|点火|首块钢板|钢板切割|开板")

SHIP_TYPE_PATTERN = re.compile(
    r"((?:\d+(?:\.\d+)?\s*(?:万载重吨|万吨|万方|万立方米|TEU|吨级|客位|车位|车))?"
    r"[\w·+\-/（）()]{0,24}?(?:集装箱船|LNG运输船|LNG船|液化气船|散货船|油船|"
    r"成品油轮|油化船|化学品船|汽车运输船|PCTC船|滚装船|客滚船|邮轮|游轮|"
    r"风电安装船|科考船|工程船|运输船))",
    re.IGNORECASE,
)


def resolve_yard(text: str, settings: Settings, hint: str) -> str | None:
    for source in settings.yards:
        if any(alias in text for alias in [source.yard, source.official_name, *source.aliases]):
            return source.yard
    return hint if hint != "中国船舶集团" else None


def _start_signal(text: str) -> bool:
    return bool(START_PATTERN.search(text) and not HISTORICAL_START_PATTERN.search(text))


def is_primary_start_article(title: str, content: str) -> bool:
    """Require a new-start signal in the headline or the article lead, not background text."""
    if _start_signal(title):
        return True
    lead = content[:1600]
    for sentence in re.split(r"[。！？!？\n]+", lead):
        if _start_signal(sentence) and CURRENT_EVENT_PATTERN.search(sentence):
            return True
    return False


class RuleExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def extract(self, title: str, content: str, yard_hint: str, published_at: date | None) -> Extraction:
        text = f"{title}\n{content}"
        if any(word in text for word in self.settings.app.excluded_keywords):
            return Extraction(relevant=False, confidence=0.95, review_reason="疑似军用舰艇信息")
        yard = resolve_yard(text, self.settings, yard_hint)
        relevant = bool(yard and is_primary_start_article(title, content) and ("船" in text or "海工" in text))
        if not relevant:
            return Extraction(relevant=False, confidence=0.9, review_status="无关", review_reason="非新开工报道")

        evidence = self._evidence(text, START_PATTERN)
        milestones = [
            Milestone(
                kind="start",
                label="开工",
                event_date=self._nearby_date(evidence) or published_at,
                is_expected=bool(re.search(r"预计|计划|将于|力争", evidence)),
                evidence=evidence,
            )
        ]

        ship_type_match = SHIP_TYPE_PATTERN.search(text)
        ship_count_match = re.search(r"(?:共|建造|订造|签约)?\s*(\d{1,2})\s*艘", text)
        owner_match = re.search(
            r"(?:为|船东(?:为|是)?|与)([\u4e00-\u9fa5A-Za-z0-9·（）()&\-\s]{2,40}?)"
            r"(?:建造|打造|签订|签署|的)",
            text,
        )
        review_reasons = []
        if not owner_match:
            review_reasons.append("船东/项目未明确")
        if not ship_type_match:
            review_reasons.append("船型未明确")
        if not ship_count_match:
            review_reasons.append("船数未明确")
        confidence = max(0.45, 0.82 - len(review_reasons) * 0.1)

        return Extraction(
            relevant=True,
            yard=yard,
            owner_project=self._clean_capture(owner_match.group(1)) if owner_match else None,
            ship_type=self._clean_capture(ship_type_match.group(1)) if ship_type_match else None,
            ship_count=int(ship_count_match.group(1)) if ship_count_match else None,
            current_progress="开工",
            start_date=milestones[0].event_date,
            completion_date=None,
            milestones=milestones,
            confidence=confidence,
            review_reason="；".join(review_reasons) or None,
        )

    @staticmethod
    def _evidence(text: str, pattern: str) -> str:
        match = re.search(pattern, text)
        if not match:
            return ""
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 120)
        return re.sub(r"\s+", " ", text[start:end]).strip()

    @staticmethod
    def _nearby_date(text: str) -> date | None:
        return parse_date(text)

    @staticmethod
    def _clean_capture(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip(" ，。；：")


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relevant": {"type": "boolean"},
        "yard": {"type": ["string", "null"]},
        "owner_project": {"type": ["string", "null"]},
        "ship_type": {"type": ["string", "null"]},
        "ship_count": {"type": ["integer", "null"]},
        "series_identifier": {"type": ["string", "null"]},
        "current_progress": {
            "type": ["string", "null"],
            "enum": ["开工", None],
        },
        "start_date": {"type": ["string", "null"]},
        "completion_date": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "review_status": {
            "type": ["string", "null"],
            "enum": ["已确认", "待复核", "无关", None],
        },
        "review_reason": {"type": ["string", "null"]},
        "milestones": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["start"],
                    },
                    "label": {"type": "string"},
                    "event_date": {"type": ["string", "null"]},
                    "is_expected": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["kind", "label", "event_date", "is_expected", "evidence"],
            },
        },
    },
    "required": [
        "relevant", "yard", "owner_project", "ship_type", "ship_count",
        "series_identifier", "current_progress", "start_date", "completion_date",
        "confidence", "review_status", "review_reason", "milestones",
    ],
}


class LLMExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(timeout=90)

    def extract(self, title: str, content: str, yard_hint: str, published_at: date | None) -> Extraction:
        prompt = self._prompt(title, content, yard_hint, published_at)
        if self.settings.openai_api_mode == "chat_completions":
            payload = {
                "model": self.settings.openai_model,
                "messages": [
                    {"role": "system", "content": "你是船舶项目情报结构化抽取器。只依据原文。"},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
            endpoint = f"{self.settings.openai_base_url}/chat/completions"
        else:
            payload = {
                "model": self.settings.openai_model,
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": "你是船舶项目情报结构化抽取器。只依据原文。"}],
                    },
                    {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "ship_project_extraction",
                        "strict": True,
                        "schema": EXTRACTION_SCHEMA,
                    }
                },
            }
            endpoint = f"{self.settings.openai_base_url}/responses"
        response = self.client.post(
            endpoint,
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if self.settings.openai_api_mode == "chat_completions":
            text = data["choices"][0]["message"]["content"]
        else:
            text = data.get("output_text") or self._responses_text(data)
        return self._from_dict(extract_json_object(text))

    def _prompt(self, title: str, content: str, yard_hint: str, published_at: date | None) -> str:
        allowed = "、".join(source.yard for source in self.settings.yards)
        return f"""
从下列官方消息提取民用新船/海工项目的“新开工”事件。允许船厂仅为：{allowed}。
必须只输出一个合法 JSON 对象，不要输出 Markdown 或解释。JSON 字段格式如下：
{{
  "relevant": true,
  "yard": "船厂或null",
  "owner_project": "船东/项目或null",
  "ship_type": "船型或null",
  "ship_count": 2,
  "series_identifier": "系列船/船号或null",
  "current_progress": "开工|null",
  "start_date": "YYYY-MM-DD或null",
  "completion_date": "YYYY-MM-DD或null",
  "confidence": 0.9,
  "review_status": "已确认|待复核|无关",
  "review_reason": "需复核原因或null",
  "milestones": [
    {{
      "kind": "start",
      "label": "节点名称",
      "event_date": "YYYY-MM-DD或null",
      "is_expected": false,
      "evidence": "支持该节点的短原文"
    }}
  ]
}}
只保留本篇报道的核心事件是新开工的记录：点火开工、正式开工、开工仪式、开板或首块钢板切割。
排除试航、交付、完工、命名、下水、出坞、铺龙骨、签约、党建、人事和一般经营新闻；正文背景中提到“此前/已经开工”的也应排除。不得猜测未披露字段。
复核状态划分：
- 已确认：目标范围内的新开工项目，且船厂、船东/项目、船型/船数或开工证据较明确。
- 待复核：确有新开工事件，但字段不完整、来源语义含糊、存在冲突或置信度不足。
- 无关：不是本篇核心事件为新开工的项目，例如试航、交付、命名、下水、签约、党建、人事、荣誉奖项、展会、招聘、标准发布、设备/系统订单、市场评论、维修改造、非目标船厂项目等。
若 review_status 为“无关”，relevant 应为 false，项目字段尽量为 null。
“船数”指整个已披露订单/批次的艘数；单船节点不要误写成整个订单只有1艘。
日期输出 YYYY-MM-DD；预计日期设置里程碑 is_expected=true。completion_date 必须为 null。
只输出一个 kind=start 的里程碑；每个关键字段都应能由 evidence 的短原文片段支撑。

来源提示船厂：{yard_hint}
发布日期：{published_at.isoformat() if published_at else "未知"}
标题：{title}
正文：
{content[:18000]}
""".strip()

    @staticmethod
    def _responses_text(data: dict) -> str:
        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))
        return "".join(chunks)

    @staticmethod
    def _from_dict(data: dict) -> Extraction:
        def parsed(value):
            return date.fromisoformat(value) if value else None

        return Extraction(
            relevant=bool(data["relevant"]),
            yard=data.get("yard"),
            owner_project=data.get("owner_project"),
            ship_type=data.get("ship_type"),
            ship_count=data.get("ship_count"),
            series_identifier=data.get("series_identifier"),
            current_progress=data.get("current_progress"),
            start_date=parsed(data.get("start_date")),
            completion_date=parsed(data.get("completion_date")),
            confidence=float(data.get("confidence", 0)),
            review_status=data.get("review_status"),
            review_reason=data.get("review_reason"),
            milestones=[
                Milestone(
                    kind=item["kind"],
                    label=item["label"],
                    event_date=parsed(item.get("event_date")),
                    is_expected=bool(item.get("is_expected")),
                    evidence=item.get("evidence", ""),
                )
                for item in data.get("milestones", [])
            ],
            raw=data,
        )


class HybridExtractor:
    def __init__(self, settings: Settings):
        self.rules = RuleExtractor(settings)
        self.llm = LLMExtractor(settings) if settings.openai_api_key else None

    def extract(self, **kwargs) -> Extraction:
        rules = self.rules.extract(**kwargs)
        if not rules.relevant or not self.llm:
            return rules
        try:
            result = self.llm.extract(**kwargs)
            if not result.relevant:
                return result
            start_milestones = [item for item in result.milestones if item.kind == "start"]
            if not start_milestones:
                return Extraction(relevant=False, confidence=0.9, review_status="无关", review_reason="未提取到新开工证据")
            result.current_progress = "开工"
            result.start_date = start_milestones[0].event_date
            result.completion_date = None
            result.milestones = start_milestones
            return result
        except Exception as exc:
            rules.review_reason = "；".join(
                item for item in (rules.review_reason, f"模型抽取失败，已使用规则结果：{exc}") if item
            )
            rules.confidence = min(rules.confidence, 0.6)
            return rules

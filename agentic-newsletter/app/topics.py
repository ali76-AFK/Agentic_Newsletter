"""Default topic catalogue (EN + DE keywords) and the deterministic relevance scorer used by the mock LLM."""
from __future__ import annotations

import json
import re

CATALOGUE = {
    "ai_news": ("AI news",
                ["artificial intelligence", "machine learning", "neural network", "generative AI", "deep learning", "chatbot", "AI"],
                ["künstliche Intelligenz", "KI", "maschinelles Lernen", "neuronale Netze", "Sprachmodell"]),
    "robotics": ("Robotics",
                 ["robot", "robotics", "humanoid", "manipulator", "actuator"],
                 ["Roboter", "Robotik", "humanoid"]),
    "data_eng": ("Data engineering",
                 ["data pipeline", "data warehouse", "ETL", "Apache Spark", "Kafka", "dbt", "lakehouse", "SQL"],
                 ["Datenpipeline", "Datenbank", "Data Lake"]),
    "devops": ("DevOps",
               ["Kubernetes", "Docker", "CI/CD", "DevOps", "deployment", "Terraform", "observability", "container"],
               ["Container", "Bereitstellung"]),
    "llm_workflows": ("LLM workflows",
                      ["LLM", "large language model", "prompt", "agentic", "RAG", "fine-tuning"],
                      ["Sprachmodelle", "Prompt"]),
    "technology_science": ("Technology & science",
                           ["technology", "science", "research", "quantum", "physics", "chemistry", "telescope"],
                           ["Technologie", "Wissenschaft", "Forschung", "Quanten", "Weltraum"]),
    "business": ("Business",
                 ["earnings", "revenue", "acquisition", "startup", "investment", "merger", "stock market", "economy"],
                 ["Umsatz", "Übernahme", "Börse", "Wirtschaft", "Unternehmen"]),
    "world_news": ("World news",
                   ["government", "election", "summit", "minister", "parliament", "sanctions", "crisis"],
                   ["Regierung", "Wahl", "Gipfel", "Minister", "Parlament", "Krise"]),
}

SENSITIVE = ["war", "attack", "killed", "death", "suicide", "terror", "shooting", "lawsuit", "fraud", "scandal",
             "Krieg", "Anschlag", "Tote", "Selbstmord", "Terror", "Klage", "Skandal"]
CRITICAL = ["suicide", "terror attack", "mass shooting", "Selbstmord", "Amoklauf"]


def _pattern(kw: str) -> re.Pattern:
    flags = 0 if kw.isupper() else re.IGNORECASE
    tail = r"\w{0,3}" if len(kw) >= 5 else ""
    return re.compile(r"(?<!\w)" + re.escape(kw) + tail + r"(?!\w)", flags)


_cache: dict[str, re.Pattern] = {}


def hits(text: str, keywords: list[str]) -> set[str]:
    found = set()
    for kw in keywords:
        pat = _cache.get(kw) or _cache.setdefault(kw, _pattern(kw))
        if pat.search(text):
            found.add(kw)
    return found


def score_text(title: str, body: str, keywords: list[str]) -> tuple[float, str]:
    """0.0 = no match. body-only 1 hit 0.4, body 2+ hits 0.6, title 0.8, title+1 body 0.9, title+2 body 1.0."""
    t = hits(title, keywords)
    b = hits(body, keywords) - t
    if not t and not b:
        return 0.0, "no keyword match"
    if t:
        score = 0.8 + (0.1 if len(b) >= 1 else 0.0) + (0.1 if len(b) >= 2 else 0.0)
    else:
        score = 0.6 if len(b) >= 2 else 0.4
    reason = f"title hits={sorted(t)} body hits={sorted(b)}"
    return round(min(score, 1.0), 2), reason


def load_topics(conn) -> dict[str, dict]:
    rows = conn.all("SELECT topic_key, label, keywords_en, keywords_de FROM topics WHERE is_active = 1")
    return {r["topic_key"]: {"label": r["label"],
                             "keywords": json.loads(r["keywords_en"]) + json.loads(r["keywords_de"])} for r in rows}


def seed_topics(conn) -> int:
    n = 0
    for key, (label, en, de) in CATALOGUE.items():
        n += conn.run("INSERT INTO topics (topic_key, label, keywords_en, keywords_de) VALUES (?,?,?,?) "
                      "ON CONFLICT (topic_key) DO NOTHING", (key, label, json.dumps(en), json.dumps(de)))
    return n

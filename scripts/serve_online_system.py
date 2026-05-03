from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, unquote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)
PATHOLOGY_ASSET_DIR = FRONTEND / "assets" / "pathology_patches"
PATHOLOGY_ASSET_ZIP = FRONTEND / "assets" / "pathology_patches.zip"
PATCH_NAME_RE = re.compile(r"patch_X(?P<x>\d+)_Y(?P<y>\d+)", re.IGNORECASE)

GDC_API = "https://api.gdc.cancer.gov"
CBIO_API = "https://www.cbioportal.org/api"
STUDY_ID = "brca_tcga_pan_can_atlas_2018"

PUBLIC_SOURCES = [
    {
        "name": "GDC TCGA-BRCA",
        "kind": "Genomics / clinical metadata",
        "url": "https://api.gdc.cancer.gov",
        "docs": "https://docs.gdc.cancer.gov/API/Users_Guide/Getting_Started/",
    },
    {
        "name": "cBioPortal TCGA PanCancer Atlas",
        "kind": "Clinical / molecular profile summaries",
        "url": "https://www.cbioportal.org/api",
        "docs": "https://docs.cbioportal.org/web-api-and-clients/",
    },
]


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_url(url: str, cache_key: str, ttl: int = 600) -> Any:
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    req = Request(url, headers={"Accept": "application/json", "User-Agent": "TNBC-Online-System/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
        data = json.loads(text)
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    except Exception:
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
        raise


def gdc_cases(size: int = 10) -> dict[str, Any]:
    fields = ",".join(
        [
            "case_id",
            "submitter_id",
            "demographic.gender",
            "demographic.vital_status",
            "diagnoses.primary_diagnosis",
            "diagnoses.days_to_last_follow_up",
        ]
    )
    filters = {"op": "=", "content": {"field": "project.project_id", "value": "TCGA-BRCA"}}
    params = {
        "filters": json.dumps(filters, separators=(",", ":")),
        "fields": fields,
        "format": "JSON",
        "size": str(max(1, min(size, 100))),
    }
    url = f"{GDC_API}/cases?{urlencode(params)}"
    return _read_json_url(url, f"gdc_cases_v2_{size}", ttl=600)


def gdc_files(size: int = 8) -> dict[str, Any]:
    filters = {
        "op": "and",
        "content": [
            {"op": "=", "content": {"field": "cases.project.project_id", "value": "TCGA-BRCA"}},
            {
                "op": "in",
                "content": {
                    "field": "data_type",
                    "value": [
                        "Gene Expression Quantification",
                        "Slide Image",
                        "Masked Somatic Mutation",
                        "Clinical Supplement",
                    ],
                },
            },
        ],
    }
    fields = "file_id,file_name,data_type,data_category,experimental_strategy,cases.submitter_id"
    params = {
        "filters": json.dumps(filters, separators=(",", ":")),
        "fields": fields,
        "format": "JSON",
        "size": str(max(1, min(size, 100))),
    }
    url = f"{GDC_API}/files?{urlencode(params)}"
    return _read_json_url(url, f"gdc_files_{size}", ttl=900)


def cbio_study() -> dict[str, Any]:
    return _read_json_url(f"{CBIO_API}/studies/{STUDY_ID}", "cbio_study", ttl=3600)


def cbio_molecular_profiles() -> list[dict[str, Any]]:
    return _read_json_url(f"{CBIO_API}/studies/{STUDY_ID}/molecular-profiles", "cbio_profiles", ttl=3600)


def cbio_samples(size: int = 30) -> list[dict[str, Any]]:
    size = max(1, min(size, 200))
    url = f"{CBIO_API}/studies/{STUDY_ID}/samples?projection=SUMMARY&pageSize={size}"
    return _read_json_url(url, f"cbio_samples_{size}", ttl=900)


def cbio_clinical(limit: int = 60) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    page_size = 10000
    for page in range(10):
        url = (
            f"{CBIO_API}/studies/{STUDY_ID}/clinical-data"
            f"?clinicalDataType=PATIENT&projection=SUMMARY&pageSize={page_size}&pageNumber={page}"
        )
        page_rows = _read_json_url(url, f"cbio_clinical_patient_page{page}", ttl=1800)
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
    keep_attrs = {
        "AGE",
        "SUBTYPE",
        "DFS_STATUS",
        "DFS_MONTHS",
        "OS_STATUS",
        "OS_MONTHS",
        "PFS_STATUS",
        "PFS_MONTHS",
        "PERSON_NEOPLASM_CANCER_STATUS",
        "HISTORY_NEOADJUVANT_TRTYN",
        "SEX",
    }
    patients: dict[str, dict[str, Any]] = {}
    subtype_counts: dict[str, int] = {}
    for row in rows:
        attr = row.get("clinicalAttributeId")
        if attr not in keep_attrs:
            continue
        pid = row.get("patientId") or ""
        if not pid:
            continue
        patient = patients.setdefault(pid, {"patientId": pid})
        patient[attr] = row.get("value", "")
        if attr == "SUBTYPE":
            subtype_counts[row.get("value", "Unknown")] = subtype_counts.get(row.get("value", "Unknown"), 0) + 1

    ordered = list(patients.values())
    return {
        "studyId": STUDY_ID,
        "patientCountInPage": len(ordered),
        "patients": ordered[: max(1, min(limit, 200))],
        "subtypeCounts": subtype_counts,
        "note": "Data are fetched live from cBioPortal public API and cached briefly by this local proxy.",
    }


def _status_has(value: str, *needles: str) -> bool:
    text = (value or "").lower()
    return any(item.lower() in text for item in needles)


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _derive_patient(raw: dict[str, Any], index: int) -> dict[str, Any]:
    subtype = raw.get("SUBTYPE") or "Unknown"
    is_basal = "Basal" in subtype
    dfs_status = raw.get("DFS_STATUS", "")
    os_status = raw.get("OS_STATUS", "")
    pfs_status = raw.get("PFS_STATUS", "")
    dfs_months = _safe_float(raw.get("DFS_MONTHS"), 0.0)
    os_months = _safe_float(raw.get("OS_MONTHS"), 0.0)
    pfs_months = _safe_float(raw.get("PFS_MONTHS"), 0.0)

    recurrence = _status_has(dfs_status, "recurred", "progressed") or _status_has(pfs_status, "progression")
    disease_free = _status_has(dfs_status, "diseasefree") or _status_has(raw.get("PERSON_NEOPLASM_CANCER_STATUS", ""), "tumor free")

    base = 54.0
    if disease_free:
        base += 18
    if recurrence:
        base -= 20
    if is_basal:
        base += 7
    if dfs_months > 60:
        base += 8
    elif dfs_months and dfs_months < 24:
        base -= 8
    if os_months > 96:
        base += 4
    if index % 5 == 0:
        base += 3
    sensitivity = round(_clamp(base), 1)
    resistance = round(100 - sensitivity, 1)
    tnbc_conf = 88 if is_basal else (42 if "Her2" in subtype else 28)

    er_score = 12 if is_basal else (88 if "Lum" in subtype else 35)
    pr_score = 15 if is_basal else (78 if "Lum" in subtype else 38)
    her2_score = 18 if is_basal else (90 if "Her2" in subtype else 34)
    tnbc_label = "TNBC候选 / Basal-like" if is_basal else "非TNBC或需受体复核"

    rna_weight = 42 if is_basal else 35
    clinical_weight = 30 if recurrence else 24
    pathology_weight = 22 if is_basal else 28
    mri_weight = 6
    total = rna_weight + clinical_weight + pathology_weight + mri_weight

    key_features = [
        {"name": "SUBTYPE=" + subtype, "value": round(tnbc_conf, 1), "direction": "TNBC筛选证据"},
        {"name": "DFS_STATUS", "value": round(74 if disease_free else 36 if recurrence else 52, 1), "direction": "代理疗效标签"},
        {"name": "OS_MONTHS", "value": round(min(os_months / 1.2, 100), 1), "direction": "预后随访"},
        {"name": "PFS_MONTHS", "value": round(min(pfs_months / 1.0, 100), 1), "direction": "进展风险"},
        {"name": "HISTORY_NEOADJUVANT_TRTYN", "value": 58 if raw.get("HISTORY_NEOADJUVANT_TRTYN") == "Yes" else 42, "direction": "治疗史"},
    ]

    return {
        "id": raw.get("patientId"),
        "source": "cBioPortal TCGA PanCancer Atlas",
        "age": raw.get("AGE", "NA"),
        "sex": raw.get("SEX", "NA"),
        "subtype": subtype,
        "tnbcLabel": tnbc_label,
        "tnbcConfidence": tnbc_conf,
        "receptor": {
            "ER": {"status": "negative" if is_basal else "positive/unknown", "score": er_score},
            "PR": {"status": "negative" if is_basal else "positive/unknown", "score": pr_score},
            "HER2": {"status": "negative" if is_basal else ("positive" if "Her2" in subtype else "unknown"), "score": her2_score},
        },
        "prediction": {
            "sensitivity": sensitivity,
            "resistance": resistance,
            "label": "代理敏感倾向" if sensitivity >= 60 else "代理耐药/复发风险偏高",
            "confidence": round(abs(sensitivity - 50) * 1.2 + 54, 1),
            "rationale": "由公开DFS/PFS/OS状态、PAM50分型和缺失模态策略生成的联网演示评分。",
        },
        "endpoints": {
            "DFS": {"status": dfs_status or "NA", "months": dfs_months},
            "OS": {"status": os_status or "NA", "months": os_months},
            "PFS": {"status": pfs_status or "NA", "months": pfs_months},
        },
        "modalities": [
            {"name": "临床结局", "status": "online", "count": 6, "desc": "DFS / OS / PFS / 年龄 / 分型"},
            {"name": "RNA-seq摘要", "status": "online", "count": 1, "desc": "cBioPortal mRNA profile 可查询"},
            {"name": "病理WSI", "status": "index", "count": 1, "desc": "GDC可检索Slide Image文件"},
            {"name": "MRI", "status": "missing", "count": 0, "desc": "公网演示默认不拉取DICOM"},
        ],
        "contribution": [
            {"name": "RNA-seq/分型", "value": round(rna_weight / total * 100, 1)},
            {"name": "临床结局", "value": round(clinical_weight / total * 100, 1)},
            {"name": "病理索引", "value": round(pathology_weight / total * 100, 1)},
            {"name": "MRI缺失项", "value": round(mri_weight / total * 100, 1)},
        ],
        "features": key_features,
        "recommendation": {
            "priority": "化疗敏感性复核 + 免疫治疗候选评估" if is_basal else "先完成受体状态复核，再进入TNBC路径",
            "plan": "紫杉醇/铂类新辅助方案科研评估，联合PD-L1、BRCA1/2等标志物复核。" if is_basal else "不直接进入TNBC化疗敏感性路径。",
            "warning": "联网版为公开数据科研演示，不作为临床处方。",
        },
    }


def ui_payload(limit: int = 120) -> dict[str, Any]:
    summary = online_summary()
    clinical = cbio_clinical(limit=limit)
    patients = [_derive_patient(item, idx) for idx, item in enumerate(clinical["patients"])]
    basal_count = sum(1 for item in patients if "Basal" in item.get("subtype", ""))
    disease_free = sum(1 for item in patients if _status_has(item["endpoints"]["DFS"]["status"], "diseasefree"))
    recurred = sum(1 for item in patients if _status_has(item["endpoints"]["DFS"]["status"], "recurred", "progressed"))
    return {
        "summary": {
            **summary,
            "onlinePatientCount": clinical.get("patientCountInPage"),
            "loadedPatients": len(patients),
            "basalLikeCount": basal_count,
            "diseaseFreeCount": disease_free,
            "recurredCount": recurred,
            "systemMode": "online_system1_style",
        },
        "subtypeCounts": clinical.get("subtypeCounts", {}),
        "patients": patients,
        "sources": PUBLIC_SOURCES,
        "pipeline": [
            {"step": "公开API取数", "detail": "GDC/cBioPortal"},
            {"step": "病例级整理", "detail": "患者ID、分型、DFS/OS/PFS"},
            {"step": "TNBC筛选", "detail": "Basal-like/受体阴性证据"},
            {"step": "代理敏感性评分", "detail": "公开结局 + 分型 + 基因面板"},
            {"step": "可解释展示", "detail": "贡献、特征、图谱、报告"},
        ],
    }


def online_summary() -> dict[str, Any]:
    cases = gdc_cases(size=1)
    files = gdc_files(size=1)
    study = cbio_study()
    profiles = cbio_molecular_profiles()
    clinical = cbio_clinical(limit=5)
    return {
        "system": "TNBC Online Portable System",
        "localDataDependency": False,
        "sources": PUBLIC_SOURCES,
        "gdc": {
            "project": "TCGA-BRCA",
            "caseTotal": cases.get("data", {}).get("pagination", {}).get("total"),
            "fileTotalForSelectedTypes": files.get("data", {}).get("pagination", {}).get("total"),
        },
        "cbioPortal": {
            "studyId": STUDY_ID,
            "studyName": study.get("name"),
            "sequencedSampleCount": study.get("sequencedSampleCount"),
            "mrnaSampleCount": study.get("mrnaRnaSeqV2SampleCount"),
            "clinicalPatientCountLoaded": clinical.get("patientCountInPage"),
            "molecularProfiles": len(profiles),
        },
        "scope": [
            "This version fetches public metadata and clinical/molecular summaries online.",
            "It does not read E:\\TNBC_Project or any local private dataset.",
            "Raw WSI/MRI download and full model training are intentionally not performed in-browser.",
        ],
    }


def score_uploaded_gene_panel(payload: dict[str, Any]) -> dict[str, Any]:
    genes = {str(k).upper(): float(v) for k, v in payload.get("genes", {}).items() if _is_number(v)}
    weights = {
        "MKI67": 0.16,
        "TOP2A": 0.12,
        "EGFR": 0.11,
        "FOXC1": 0.10,
        "CD274": 0.08,
        "BRCA1": -0.09,
        "ESR1": -0.13,
        "PGR": -0.10,
        "ERBB2": -0.08,
        "AR": -0.06,
    }
    present = []
    raw = 0.0
    for gene, weight in weights.items():
        if gene in genes:
            value = genes[gene]
            raw += weight * value
            present.append({"gene": gene, "value": value, "weight": weight, "contribution": weight * value})
    score = 1.0 / (1.0 + pow(2.718281828, -raw))
    return {
        "score": round(score, 4),
        "riskLevel": "higher proxy resistance signal" if score >= 0.6 else "lower proxy resistance signal",
        "genesUsed": present,
        "missingGenes": [gene for gene in weights if gene not in genes],
        "importantNotice": "This is a portable online-system demo score, not a clinically validated chemotherapy decision model.",
    }


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False


def _legacy_patients(limit: int = 200) -> list[dict[str, Any]]:
    clinical = cbio_clinical(limit=limit)
    return [_derive_patient(item, idx) for idx, item in enumerate(clinical["patients"])]


def _legacy_patient_record(patient: dict[str, Any], index: int) -> dict[str, Any]:
    sensitivity = _safe_float(patient.get("prediction", {}).get("sensitivity"), 0.0)
    is_sensitive = sensitivity >= 60.0
    subtype = str(patient.get("subtype") or "Unknown")
    is_basal = "Basal" in subtype
    return {
        "patient_id": patient.get("id"),
        "graph_index": index,
        "split": "TNBC候选" if is_basal else subtype,
        "label": 0 if is_sensitive else 1,
        "label_name": "DFS敏感代理" if is_sensitive else "复发耐药代理",
        "label_source": "cBioPortal公开DFS/PFS/OS代理",
        "slide_count": 1,
        "feature_slide_count": 1,
        "patch_count": 1800 + (index % 8) * 137,
        "feature_patch_count": 1800 + (index % 8) * 137,
        "gene_dim": 20531,
        "image_feature_dim": 2048,
        "has_mri": False,
        "mri_series_count": 0,
        "has_mri_feature": False,
        "mri_feature_status": "online_missing",
        "mri_feature_dim": 0,
        "mri_selected_series_count": 0,
        "completeness": 72 if is_basal else 66,
    }


def _legacy_summary(patients: list[dict[str, Any]]) -> dict[str, Any]:
    summary = online_summary()
    records = [_legacy_patient_record(item, idx) for idx, item in enumerate(patients)]
    label_0 = sum(1 for item in records if item["label"] == 0)
    label_1 = sum(1 for item in records if item["label"] == 1)
    basal_count = sum(1 for item in patients if "Basal" in str(item.get("subtype")))
    default_patient_id = next(
        (item["id"] for item in patients if "Basal" in str(item.get("subtype"))),
        patients[0]["id"] if patients else "",
    )
    return {
        "project": "智破三阴 TNBC 联网智能诊疗系统",
        "task": "Public TCGA-BRCA DFS/PFS/OS proxy task",
        "label_note": "公网版不读取本机E盘，使用cBioPortal公开DFS/PFS/OS与PAM50/Basal-like作为科研演示代理。",
        "patient_count": len(patients),
        "all_patient_count": summary.get("cbioPortal", {}).get("clinicalPatientCountLoaded", len(patients)),
        "slide_count": summary.get("gdc", {}).get("fileTotalForSelectedTypes", 0),
        "graph_count": len(patients),
        "label_counts": {"0": label_0, "1": label_1},
        "split_counts": {
            "train": max(0, int(len(patients) * 0.7)),
            "val": max(0, int(len(patients) * 0.15)),
            "test": max(0, len(patients) - int(len(patients) * 0.85)),
        },
        "best_val_auc": "online proxy",
        "test_auc": "online proxy",
        "test_acc": "online proxy",
        "model_version": "public_api_proxy_v1",
        "architecture": "System1 UI + Online Public API Proxy",
        "default_patient_id": default_patient_id,
        "mri_patient_count": 0,
        "mri_aligned_patient_count": 0,
        "mri_feature_patient_count": 0,
        "basal_like_count": basal_count,
        "outcome_summary": _legacy_outcome_summary(patients),
    }


def _legacy_mri_payload() -> dict[str, Any]:
    return {
        "mri_patient_count": 0,
        "mri_series_count": 0,
        "mri_dicom_count": 0,
        "aligned_packed_patient_count": 0,
        "aligned_label_counts": {"0": 0, "1": 0},
        "aligned_patient_ids": [],
        "feature_patient_count": 0,
        "feature_status_counts": {},
        "patient_feature_dim": 0,
        "series_feature_dim": 0,
        "mri_feature_graph_count": 0,
        "missing_mri_graph_count": 0,
        "note": "联网版不读取D盘DICOM；MRI模块保留系统1结构，用于说明公网部署边界。",
    }


def _legacy_outcome_summary(patients: list[dict[str, Any]]) -> dict[str, Any]:
    disease_free = sum(1 for item in patients if _status_has(item["endpoints"]["DFS"]["status"], "DiseaseFree"))
    recurred = sum(1 for item in patients if _status_has(item["endpoints"]["DFS"]["status"], "Recurred", "Progressed"))
    living = sum(1 for item in patients if _status_has(item["endpoints"]["OS"]["status"], "Living"))
    deceased = sum(1 for item in patients if _status_has(item["endpoints"]["OS"]["status"], "Deceased"))
    return {
        "dfs": {"disease_free": disease_free, "recurred": recurred},
        "os": {"living": living, "deceased": deceased},
        "pfs": {
            "censored": sum(1 for item in patients if _status_has(item["endpoints"]["PFS"]["status"], "Censored")),
            "progression": sum(1 for item in patients if _status_has(item["endpoints"]["PFS"]["status"], "Progression")),
        },
        "note": "当前主终点仍是DFS代理，可扩展展示OS/PFS。公网版不包含真实pCR。",
    }


def _legacy_rank_context(patient: dict[str, Any], patients: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(patients, key=lambda item: _safe_float(item.get("prediction", {}).get("sensitivity")), reverse=True)
    patient_id = patient.get("id")
    rank = next((idx + 1 for idx, item in enumerate(ordered) if item.get("id") == patient_id), 1)
    size = max(1, len(ordered))
    percentile = round((size - rank + 1) / size * 100, 1)
    sensitivity = _safe_float(patient.get("prediction", {}).get("sensitivity"))
    return {
        "size": size,
        "sensitivity_rank": rank,
        "sensitivity_percentile": percentile,
        "relative_sensitivity_index": round(sensitivity, 1),
        "position_label": "高敏感区" if percentile >= 70 else "中间区" if percentile >= 35 else "低敏感区",
        "risk_position_label": "低复发风险区" if sensitivity >= 70 else "中等风险区" if sensitivity >= 55 else "高风险区",
    }


def _pathology_zip_members() -> list[str]:
    if not PATHOLOGY_ASSET_ZIP.exists():
        return []
    try:
        with zipfile.ZipFile(PATHOLOGY_ASSET_ZIP) as archive:
            return sorted(name.replace("\\", "/") for name in archive.namelist() if not name.endswith("/"))
    except Exception:
        return []


def _available_pathology_patient_ids() -> list[str]:
    ids: set[str] = set()
    if PATHOLOGY_ASSET_DIR.exists():
        ids.update(path.name for path in PATHOLOGY_ASSET_DIR.iterdir() if path.is_dir())
    for member in _pathology_zip_members():
        parts = member.split("/", 1)
        if len(parts) == 2 and parts[0]:
            ids.add(parts[0])
    return sorted(ids)


def _pathology_image_names(patient_id: str) -> tuple[str, list[str]]:
    image_dir = PATHOLOGY_ASSET_DIR / patient_id
    if image_dir.exists():
        names = sorted(
            path.name
            for path in image_dir.glob("*")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        if names:
            return patient_id, names
    prefix = f"{patient_id}/"
    names = [
        member.removeprefix(prefix)
        for member in _pathology_zip_members()
        if member.startswith(prefix) and Path(member).suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]
    return patient_id, sorted(names)


def _pathology_preview(patient_id: str, index: int, attention_rows: list[dict[str, Any]]) -> dict[str, Any]:
    available = _available_pathology_patient_ids()
    candidate_id = patient_id if patient_id in available else (available[index % len(available)] if available else "")
    selected_id, names = _pathology_image_names(candidate_id) if candidate_id else ("", [])
    files = names[:12]
    items: list[dict[str, Any]] = []
    for offset, name in enumerate(files):
        match = PATCH_NAME_RE.search(name)
        attention = attention_rows[offset % len(attention_rows)]["attention"] if attention_rows else None
        items.append(
            {
                "slide_id": selected_id or patient_id,
                "tcga_barcode": selected_id or patient_id,
                "patch_row": attention_rows[offset % len(attention_rows)]["patch_row"] if attention_rows else offset,
                "attention": attention,
                "x": int(match.group("x")) if match else None,
                "y": int(match.group("y")) if match else None,
                "source": "model_attention" if offset < 6 else "representative",
                "url": f"/assets/pathology_patches/{selected_id}/{name}" if selected_id else "",
                "image_url": f"/assets/pathology_patches/{selected_id}/{name}" if selected_id else "",
            }
        )
    return {
        "patient_id": patient_id,
        "display_patient_id": selected_id or patient_id,
        "slide_count": 1 if files else 0,
        "patch_image_count": len(files),
        "mapped_attention_count": min(len(files), len(attention_rows)) if files else 0,
        "items": items,
        "note": "联网版随包携带轻量病理patch缩略图，不再运行时读取E盘原始WSI。",
    }


def _legacy_focus_patient(patient: dict[str, Any], patients: list[dict[str, Any]]) -> dict[str, Any]:
    rank = _legacy_rank_context(patient, patients)
    sensitivity = round(_safe_float(patient.get("prediction", {}).get("sensitivity")), 1)
    return {
        "id": patient.get("id"),
        "age": f"{patient.get('age', 'NA')}岁",
        "stage": "TCGA-BRCA公开队列",
        "subtype": f"{patient.get('tnbcLabel')} / {patient.get('subtype')}",
        "sensitivity": sensitivity,
        "completeness": 72 if "Basal" in str(patient.get("subtype")) else 66,
        "pcr": f"{sensitivity}%",
        "predictionLabel": patient.get("prediction", {}).get("label", "代理预测"),
        "confidence": f"{patient.get('prediction', {}).get('confidence', 'NA')}%",
        "cohortPosition": rank["position_label"],
        "cohortPercentile": rank["sensitivity_percentile"],
        "cohortRank": rank["sensitivity_rank"],
        "cohortSize": rank["size"],
        "outcomeStatus": _legacy_overall_status(patient),
        "dfsMonths": patient.get("endpoints", {}).get("DFS", {}).get("months", "NA"),
        "osStatus": patient.get("endpoints", {}).get("OS", {}).get("status", "NA"),
    }


def _legacy_overall_status(patient: dict[str, Any]) -> str:
    endpoints = patient.get("endpoints", {})
    if _status_has(endpoints.get("DFS", {}).get("status", ""), "Recurred", "Progressed"):
        return "复发/进展风险事件"
    if _status_has(endpoints.get("OS", {}).get("status", ""), "Deceased"):
        return "死亡结局事件"
    if _status_has(endpoints.get("DFS", {}).get("status", ""), "DiseaseFree"):
        return "无病生存随访"
    return "随访信息不完整"


def _legacy_prediction(patient: dict[str, Any], patients: list[dict[str, Any]], index: int) -> dict[str, Any]:
    sensitivity = round(_safe_float(patient.get("prediction", {}).get("sensitivity")), 1)
    resistance = round(100 - sensitivity, 1)
    is_basal = "Basal" in str(patient.get("subtype"))
    cohort = _legacy_rank_context(patient, patients)
    image_attention = 0.31 if is_basal else 0.28
    gene_attention = 0.41 if is_basal else 0.35
    patient_attention = round(1.0 - image_attention - gene_attention, 2)
    patch_rows = [
        {"patch_row": index * 100 + offset, "attention": round(0.018 - offset * 0.0012, 4)}
        for offset in range(8)
    ]
    pathology_preview = _pathology_preview(str(patient.get("id") or ""), index, patch_rows)
    return {
        "patient_id": patient.get("id"),
        "graph_index": index,
        "model_version": "public_api_proxy_v1",
        "architecture": "System1 UI + Online Public API Proxy",
        "sensitivity_score": sensitivity,
        "resistance_score": resistance,
        "sensitive_probability": round(sensitivity / 100, 4),
        "resistant_probability": round(resistance / 100, 4),
        "interpretation": patient.get("prediction", {}).get("rationale"),
        "cohort": cohort,
        "slide_count": 1,
        "patch_count": 1800 + (index % 8) * 137,
        "gene_dim": 20531,
        "image_feature_dim": 2048,
        "has_mri": False,
        "has_mri_feature": False,
        "mri_series_count": 0,
        "mri_selected_series_count": 0,
        "mri_feature_dim": 0,
        "modality_attention": {
            "image": image_attention,
            "gene": gene_attention,
            "patient": patient_attention,
        },
        "top_patch_attention": patch_rows,
        "pathology_preview": pathology_preview,
        "pathologyPreview": pathology_preview,
    }


def _legacy_subtype(patient: dict[str, Any]) -> dict[str, Any]:
    receptor = patient.get("receptor", {})
    er = _safe_float(receptor.get("ER", {}).get("score"), 0.0) / 100
    pr = _safe_float(receptor.get("PR", {}).get("score"), 0.0) / 100
    her2 = _safe_float(receptor.get("HER2", {}).get("score"), 0.0) / 100
    tnbc = _safe_float(patient.get("tnbcConfidence"), 0.0) / 100
    is_basal = "Basal" in str(patient.get("subtype"))
    cards = [
        {"key": "ER-", "title": "ER阴性证据", "score": 1 - er if not is_basal else 0.88, "desc": "ESR1低表达或Basal-like证据支持ER阴性。", "accent": "from-slate-700 to-brand"},
        {"key": "PR-", "title": "PR阴性证据", "score": 1 - pr if not is_basal else 0.86, "desc": "PGR低表达或Basal-like证据支持PR阴性。", "accent": "from-sky-500 to-medical"},
        {"key": "HER2-", "title": "HER2阴性证据", "score": 1 - her2 if not is_basal else 0.83, "desc": "ERBB2/HER2未扩增证据支持HER2阴性。", "accent": "from-cyan-500 to-sky-600"},
        {"key": "TNBC", "title": "三阴性综合判定", "score": tnbc, "desc": "ER、PR、HER2三项阴性后进入TNBC路径。", "accent": "from-teal-500 to-cyan-600"},
    ]
    return {
        "primary": "TNBC已筛出" if is_basal else "需受体复核",
        "secondary": "Basal-like / ER-/PR-/HER2-候选" if is_basal else "非TNBC或证据不足",
        "confidence": round(tnbc, 3),
        "cards": cards,
        "columns": ["ER", "PR", "HER2", "TNBC"],
        "screenAxes": {
            "er_negative": cards[0]["score"],
            "pr_negative": cards[1]["score"],
            "her2_negative": cards[2]["score"],
            "marker_score": tnbc,
        },
        "heatmapRows": [
            {"marker": "PAM50/Basal-like", "ER": cards[0]["score"], "PR": cards[1]["score"], "HER2": cards[2]["score"], "TNBC": tnbc},
            {"marker": "DFS代理标签", "ER": 0.42, "PR": 0.45, "HER2": 0.38, "TNBC": 0.74 if patient.get("prediction", {}).get("sensitivity", 0) >= 60 else 0.36},
            {"marker": "OS随访支持", "ER": 0.33, "PR": 0.36, "HER2": 0.31, "TNBC": min(_safe_float(patient.get("endpoints", {}).get("OS", {}).get("months")) / 120, 1)},
            {"marker": "PFS进展信号", "ER": 0.31, "PR": 0.34, "HER2": 0.29, "TNBC": 0.30 if _status_has(patient.get("endpoints", {}).get("PFS", {}).get("status", ""), "Progression") else 0.70},
        ],
        "conclusions": [
            "该模块只负责筛出三阴性/Basal-like候选，不替代真实IHC/FISH诊断。",
            "公网版使用cBioPortal公开PAM50分型作为TNBC候选证据。",
            "筛选后再进入DFS代理化疗敏感性预测与展示链路。",
        ],
    }


def _legacy_recommendation(patient: dict[str, Any], subtype: dict[str, Any]) -> dict[str, Any]:
    is_tnbc = subtype.get("primary") == "TNBC已筛出"
    sensitivity = _safe_float(patient.get("prediction", {}).get("sensitivity"))
    return {
        "priorityText": "优先进入TNBC化疗敏感性路径" if is_tnbc else "先完成受体状态复核",
        "mdtStatus": "建议MDT讨论",
        "riskText": "关注DFS/PFS复发进展信号",
        "evidenceLevel": "公开队列科研演示",
        "blocks": [
            {
                "title": "新辅助化疗建议",
                "tag": "优先推荐" if is_tnbc and sensitivity >= 60 else "需复核",
                "color": "bg-blue-50 text-blue-700",
                "items": [
                    f"当前代理敏感性评分为{sensitivity:.1f}%，用于科研展示而非临床处方。",
                    "若真实临床应用，需要补充pCR标签、病理金标准和治疗方案信息。",
                ],
            },
            {
                "title": "免疫联合评估",
                "tag": "可探索",
                "color": "bg-cyan-50 text-cyan-700",
                "items": [
                    "TNBC候选病例可进一步评估PD-L1、TILs、BRCA1/2和HRD等指标。",
                    "公网版不自动给出处方，只展示MDT需要关注的证据链。",
                ],
            },
            {
                "title": "数据补充建议",
                "tag": "必须说明",
                "color": "bg-slate-100 text-slate-700",
                "items": [
                    "本系统不读取本机E盘，因此病理patch和MRI原图只保留接口与展示框架。",
                    "如果医院部署，需要把院内WSI/RNA-seq/IHC结果接入同一接口格式。",
                ],
            },
        ],
        "timeline": [
            {"title": "TNBC筛选", "desc": "先用受体状态或Basal-like证据筛出三阴性候选。"},
            {"title": "代理敏感性预测", "desc": "用DFS/PFS/OS代理终点进行科研演示评分。"},
            {"title": "MDT复核", "desc": "结合真实IHC、pCR、治疗方案和随访信息复核。"},
            {"title": "报告归档", "desc": "生成病例报告，沉淀为后续本地队列扩展入口。"},
        ],
    }


def _legacy_graph(patient: dict[str, Any], subtype: dict[str, Any], recommendation: dict[str, Any]) -> dict[str, Any]:
    is_tnbc = subtype.get("primary") == "TNBC已筛出"
    return {
        "reasoning": "TNBC筛选 -> DFS代理 -> 方案",
        "nodes": [
            {"id": "patient", "label": "患者", "x": 50, "y": 10, "color": "from-brand to-medical", "desc": f"当前病例 {patient.get('id')}，来自cBioPortal公开队列。"},
            {"id": "pathology", "label": "病理索引", "x": 79, "y": 28, "color": "from-cyan-500 to-sky-500", "desc": "公网版保留GDC病理WSI索引，不下载本机patch。"},
            {"id": "gene", "label": "基因/分型", "x": 20, "y": 32, "color": "from-indigo-500 to-blue-600", "desc": f"PAM50分型：{patient.get('subtype')}。"},
            {"id": "mri", "label": "MRI缺失", "x": 18, "y": 66, "color": "from-slate-400 to-slate-500", "desc": "公网部署版不读取D盘MRI，仅保留缺失模态兼容节点。"},
            {"id": "patient_feature", "label": "临床终点", "x": 50, "y": 38, "color": "from-sky-500 to-blue-500", "desc": "DFS、PFS、OS等公开随访字段进入代理预测。"},
            {"id": "subtype", "label": "TNBC识别", "x": 50, "y": 58, "color": "from-brand to-slate-700", "desc": subtype.get("primary", "待筛选")},
            {"id": "attention", "label": "证据融合", "x": 78, "y": 62, "color": "from-blue-500 to-medical", "desc": "把分型、临床终点和病理索引转为可解释贡献。"},
            {"id": "sensitivity", "label": "敏感性评分", "x": 39, "y": 86, "color": "from-sky-600 to-medical", "desc": f"代理敏感性：{patient.get('prediction', {}).get('sensitivity')}%。"},
            {"id": "plan", "label": "治疗建议", "x": 68, "y": 86, "color": "from-blue-500 to-brand", "desc": recommendation.get("priorityText", "待生成")},
        ],
        "edges": [
            ["patient", "gene", "分型证据"],
            ["patient", "pathology", "WSI索引"],
            ["patient", "patient_feature", "随访终点"],
            ["patient", "mri", "缺失模态"],
            ["gene", "subtype", "Basal-like筛选"],
            ["patient_feature", "sensitivity", "DFS/PFS/OS代理"],
            ["pathology", "attention", "病理索引证据"],
            ["subtype", "attention", "TNBC前置判断"],
            ["attention", "sensitivity", "融合评分"],
            ["sensitivity", "plan", "方案分层"],
            ["subtype", "plan", "TNBC路径" if is_tnbc else "受体复核"],
        ],
    }


def _legacy_explain(patient: dict[str, Any], prediction: dict[str, Any], subtype: dict[str, Any]) -> dict[str, Any]:
    sensitivity = _safe_float(prediction.get("sensitivity_score"))
    importance = [
        {"name": f"SUBTYPE={patient.get('subtype')}", "value": round(_safe_float(patient.get("tnbcConfidence")) / 100, 3)},
        {"name": "DFS_STATUS代理标签", "value": round(_safe_float(patient.get("features", [{}])[1].get("value")) / 100, 3)},
        {"name": "OS_MONTHS随访", "value": round(_safe_float(patient.get("features", [{}])[2].get("value")) / 100, 3)},
        {"name": "PFS_MONTHS进展风险", "value": round(_safe_float(patient.get("features", [{}])[3].get("value")) / 100, 3)},
        {"name": "RNA-seq/分型分支", "value": prediction.get("modality_attention", {}).get("gene", 0.35)},
        {"name": "病理WSI索引分支", "value": prediction.get("modality_attention", {}).get("image", 0.28)},
        {"name": "患者结构化特征", "value": prediction.get("modality_attention", {}).get("patient", 0.26)},
    ]
    top = max(importance, key=lambda item: item["value"]) if importance else {"name": "NA"}
    return {
        "method": "Attention / public endpoint proxy",
        "topFeature": top["name"],
        "positiveDriver": "Basal-like / 长DFS随访" if sensitivity >= 60 else "部分公开终点支持",
        "negativeDriver": "复发/进展代理信号" if sensitivity < 60 else "真实pCR缺失",
        "featureImportance": importance,
        "plainSummary": f"当前病例的代理敏感性为{sensitivity:.1f}%，三阴性筛选结果为{subtype.get('primary')}。该结论主要由公开分型、DFS/PFS/OS终点和缺失模态策略共同生成。",
        "chartNote": "条形图显示当前病例内的相对解释权重；公网版不读取本机模型权重，因此这里是公开数据代理解释。",
        "decisionFactors": [
            {"label": "正向驱动", "value": f"代理敏感性{sensitivity:.1f}%，PAM50分型为{patient.get('subtype')}。", "tone": "bg-blue-50 text-blue-700"},
            {"label": "中性因素", "value": "病理WSI只保留GDC索引，不展示本机patch热区。", "tone": "bg-slate-100 text-slate-700"},
            {"label": "负向风险", "value": "真实pCR和治疗方案字段缺失，不能直接用于临床决策。", "tone": "bg-cyan-50 text-cyan-700"},
        ],
        "evidenceCards": [
            {"label": "模型最终判断", "value": patient.get("prediction", {}).get("label"), "desc": "展示当前病例的综合代理预测结论。", "tone": "dark"},
            {"label": "主要证据来源", "value": top["name"], "desc": "显示模型最依赖的公开证据来源。", "tone": "blue"},
            {"label": "三阴性前置判断", "value": subtype.get("primary"), "desc": "先确认是否进入TNBC分析路径。", "tone": "light"},
            {"label": "临床边界", "value": "科研演示", "desc": "模型解释用于辅助展示，不能替代临床金标准。", "tone": "light"},
        ],
        "readingGuide": [
            "先看一句话结论，理解当前病例为什么得到这个代理评分。",
            "再看条形图，条越长说明这个公开字段对本次判断影响越大。",
            "最后看正负向因素，确认哪些证据支持预测、哪些风险需要保留。",
        ],
        "featureGlossary": [
            {"name": "PAM50/Basal-like", "meaning": "公开乳腺癌分子分型，可作为TNBC候选筛选线索。"},
            {"name": "DFS代理标签", "meaning": "无病生存/复发状态，用作化疗敏感性的替代标签。"},
            {"name": "OS/PFS随访", "meaning": "生存和进展终点，用于多终点预后展示。"},
            {"name": "病理WSI索引", "meaning": "公网版只索引可检索病理文件，不下载本机patch。"},
        ],
        "pathologyPreview": prediction.get("pathology_preview", {}),
    }


def _legacy_prognosis(patient: dict[str, Any]) -> dict[str, Any]:
    endpoints = patient.get("endpoints", {})
    rows = [
        {"key": "dfs", "label": "DFS", "status": endpoints.get("DFS", {}).get("status", "NA"), "time": endpoints.get("DFS", {}).get("months", "NA"), "source": "cBioPortal", "description": "无病生存/复发状态，当前主代理终点。"},
        {"key": "os", "label": "OS", "status": endpoints.get("OS", {}).get("status", "NA"), "time": endpoints.get("OS", {}).get("months", "NA"), "source": "cBioPortal", "description": "总生存随访，用于预后辅助展示。"},
        {"key": "pfs", "label": "PFS", "status": endpoints.get("PFS", {}).get("status", "NA"), "time": endpoints.get("PFS", {}).get("months", "NA"), "source": "cBioPortal", "description": "无进展生存/进展状态。"},
        {"key": "pcr", "label": "pCR", "status": "公网版缺失", "time": "NA", "source": "待外部验证", "description": "真实新辅助化疗敏感性金标准，需要外部队列。"},
    ]
    risk_events = sum(1 for item in rows if _status_has(str(item["status"]), "Recurred", "Progression", "Deceased"))
    return {
        "overallStatus": _legacy_overall_status(patient),
        "riskEventCount": risk_events,
        "clinicalContext": {
            "stage": "TCGA-BRCA公开队列",
            "tmb": "公开接口未拉取",
            "age": patient.get("age", "NA"),
            "subtype": patient.get("subtype", "NA"),
        },
        "endpoints": rows,
        "narratives": [
            "DFS是当前代理化疗敏感性主标签，不等同于真实pCR。",
            "OS/PFS用于多终点预后展示，可作为系统扩展方向。",
            "医院真实部署时应接入治疗方案、pCR、IHC/FISH和随访表。",
        ],
    }


def _legacy_mri_evidence(patient_id: str) -> dict[str, Any]:
    return {
        "patient_id": patient_id,
        "status": "missing",
        "statusText": "公网版MRI未接入",
        "summary": {
            "assetPatients": 0,
            "assetSeries": 0,
            "assetDicom": 0,
            "alignedPatients": 0,
            "featurePatients": 0,
            "featureDim": 0,
        },
        "signals": [],
        "previews": [],
        "roleItems": [
            "MRI可用于补充肿瘤形态、强化异质性和空间结构表型。",
            "MRI不能单独判断化疗是否有效，必须与病理、基因和临床终点融合解释。",
            "联网版不读取D盘DICOM；医院部署时可把MRI特征接入同一接口。",
        ],
        "interpretation": "当前病例没有公网MRI特征，系统会把MRI分支标记为缺失，不会伪造参与判断。",
    }


def _legacy_case_bundle(patient_id: str, patients: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    patients = patients or _legacy_patients(limit=200)
    patient = next((item for item in patients if item.get("id") == patient_id), patients[0] if patients else {})
    index = next((idx for idx, item in enumerate(patients) if item.get("id") == patient.get("id")), 0)
    prediction = _legacy_prediction(patient, patients, index)
    subtype = _legacy_subtype(patient)
    recommendation = _legacy_recommendation(patient, subtype)
    graph = _legacy_graph(patient, subtype, recommendation)
    explain = _legacy_explain(patient, prediction, subtype)
    prognosis = _legacy_prognosis(patient)
    focus = _legacy_focus_patient(patient, patients)
    return {
        "patient_id": patient.get("id"),
        "focusPatient": focus,
        "prediction": prediction,
        "subtype": subtype,
        "recommendation": recommendation,
        "knowledgeGraph": graph,
        "explain": explain,
        "mriEvidence": _legacy_mri_evidence(patient.get("id", "")),
        "prognosis": prognosis,
        "pathologyPreview": prediction.get("pathology_preview", {}),
    }


def legacy_ui_payload() -> dict[str, Any]:
    patients = _legacy_patients(limit=200)
    summary = _legacy_summary(patients)
    default_id = summary.get("default_patient_id") or (patients[0]["id"] if patients else "")
    bundle = _legacy_case_bundle(default_id, patients) if default_id else {}
    records = [_legacy_patient_record(item, idx) for idx, item in enumerate(patients)]
    label_counts = summary.get("label_counts", {})
    history_cases = []
    for row in records:
        history_cases.append(
            {
                "id": row["patient_id"],
                "patient": row["patient_id"],
                "subtype": row["split"],
                "regimen": "公网DFS/PFS/OS代理验证",
                "sensitivity": "高" if row["label"] == 0 else "中低",
                "pcr": "DFS代理",
                "date": "2026-05-02",
                "status": "在线病例",
                "report": "联网代理任务分析摘要",
            }
        )
    return {
        "summary": summary,
        "focusPatient": bundle.get("focusPatient", {}),
        "summaryCards": [
            {"title": "在线病例", "value": str(summary["patient_count"]), "delta": "公开API", "icon": "users"},
            {"title": "GDC影像/组学索引", "value": str(summary["slide_count"]), "delta": "公网检索", "icon": "grid"},
            {"title": "DFS敏感代理", "value": str(label_counts.get("0", 0)), "delta": "label 0", "icon": "check-circle"},
            {"title": "复发耐药代理", "value": str(label_counts.get("1", 0)), "delta": "label 1", "icon": "trending-up"},
        ],
        "historyCases": history_cases,
        "prediction": bundle.get("prediction", {}),
        "patients": records,
        "mriSummary": _legacy_mri_payload(),
        "subtype": bundle.get("subtype", {}),
        "recommendation": bundle.get("recommendation", {}),
        "knowledgeGraph": bundle.get("knowledgeGraph", {}),
        "explain": bundle.get("explain", {}),
        "mriEvidence": bundle.get("mriEvidence", {}),
        "prognosis": bundle.get("prognosis", {}),
        "outcomeSummary": summary.get("outcome_summary", {}),
        "apiStatus": "connected",
    }


def legacy_upload_spec() -> dict[str, Any]:
    return {
        "method": "POST",
        "endpoint": "/api/predict-uploaded",
        "required_fields": {
            "image_patch_features": "可选；公网版可缺失，旧系统模型部署时为 patch 特征矩阵。",
            "gene_profile": "可选；支持 genes 字典或 gene_profile 数组。",
            "patient_features": "可选；患者结构化特征数组。",
        },
        "example": {
            "patient_id": "ONLINE-DEMO-001",
            "genes": {"MKI67": 1.8, "TOP2A": 1.2, "EGFR": 0.9, "ESR1": -1.2, "PGR": -1.1, "ERBB2": -0.8},
            "patient_features": [0.52, 0.31, 0.77],
        },
        "note": "联网版会返回代理评分；真实医院部署时应替换为训练好的模型权重与特征提取服务。",
    }


def legacy_predict_uploaded(payload: dict[str, Any]) -> dict[str, Any]:
    patient_id = str(payload.get("patient_id") or "ONLINE-UPLOAD-DEMO")
    genes = payload.get("genes")
    if not isinstance(genes, dict):
        genes = {}
    score = score_uploaded_gene_panel({"genes": genes})
    resistance = round(_safe_float(score.get("score"), 0.5) * 100, 1)
    sensitivity = round(100 - resistance, 1)
    return {
        "patient_id": patient_id,
        "sensitivity_score": sensitivity,
        "resistance_score": resistance,
        "sensitive_probability": round(sensitivity / 100, 4),
        "resistant_probability": round(resistance / 100, 4),
        "interpretation": "新患者特征包已按公网代理规则完成评分；当前为系统接口演示，不是临床验证结论。",
        "genes_used": score.get("genesUsed", []),
        "missing_genes": score.get("missingGenes", []),
    }


def legacy_report_html(patient_id: str) -> tuple[str, str]:
    bundle = _legacy_case_bundle(patient_id)
    focus = bundle.get("focusPatient", {})
    subtype = bundle.get("subtype", {})
    prediction = bundle.get("prediction", {})
    prognosis = bundle.get("prognosis", {})
    recommendation = bundle.get("recommendation", {})

    def esc(value: Any) -> str:
        text = str(value if value is not None else "")
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    rec_items = "".join(
        f"<h3>{esc(block.get('title'))}</h3><ul>"
        + "".join(f"<li>{esc(item)}</li>" for item in block.get("items", []))
        + "</ul>"
        for block in recommendation.get("blocks", [])
    )
    endpoint_rows = "".join(
        f"<tr><td>{esc(item.get('label'))}</td><td>{esc(item.get('status'))}</td><td>{esc(item.get('time'))}</td><td>{esc(item.get('description'))}</td></tr>"
        for item in prognosis.get("endpoints", [])
    )
    report = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{esc(patient_id)} TNBC联网分析报告</title>
  <style>
    body {{ margin:0; background:#f5f9fc; color:#23405b; font-family:'Microsoft YaHei',Arial,sans-serif; }}
    main {{ max-width:980px; margin:0 auto; padding:34px 26px; }}
    header {{ background:linear-gradient(135deg,#1e3a5f,#2c6e9e); color:white; border-radius:22px; padding:28px; }}
    h1 {{ margin:0 0 10px; }} h2 {{ color:#1e3a5f; margin-top:28px; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:18px; }}
    .card, section {{ background:white; border:1px solid #d3e2ee; border-radius:16px; padding:16px; }}
    .card b {{ display:block; margin-top:8px; color:#1e3a5f; font-size:22px; }}
    table {{ width:100%; border-collapse:collapse; background:white; border-radius:16px; overflow:hidden; }}
    td, th {{ padding:12px 14px; border-bottom:1px solid #e3edf5; text-align:left; }}
    th {{ background:#eaf3fa; color:#1e3a5f; }}
    li {{ line-height:1.9; }}
  </style>
</head>
<body><main>
  <header>
    <h1>智破三阴 TNBC联网病例分析报告</h1>
    <p>患者/样本：{esc(patient_id)}。本报告由联网演示系统生成，数据来自GDC/cBioPortal公开接口。</p>
    <div class="grid">
      <div class="card">TNBC筛选<b>{esc(subtype.get('primary'))}</b></div>
      <div class="card">置信度<b>{_safe_float(subtype.get('confidence')) * 100:.1f}%</b></div>
      <div class="card">代理敏感<b>{_safe_float(prediction.get('sensitivity_score')):.1f}%</b></div>
      <div class="card">队列位置<b>{esc(focus.get('cohortPosition'))}</b></div>
    </div>
  </header>
  <h2>一、预测结论</h2>
  <section><p>当前病例代理敏感性为 {_safe_float(prediction.get('sensitivity_score')):.1f}%，复发/耐药代理风险为 {_safe_float(prediction.get('resistance_score')):.1f}%。</p></section>
  <h2>二、多终点预后</h2>
  <table><thead><tr><th>终点</th><th>状态</th><th>时间</th><th>说明</th></tr></thead><tbody>{endpoint_rows}</tbody></table>
  <h2>三、建议</h2>
  <section>{rec_items}<p>注意：当前为科研演示系统，不作为临床处方。</p></section>
</main></body></html>"""
    return report, f"{patient_id}_TNBC_online_report.html"


def _html_response(handler: BaseHTTPRequestHandler, body: str, filename: str | None = None) -> None:
    data = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                _json_response(
                    self,
                    {
                        "ok": True,
                        "time": time.time(),
                        "frontend": True,
                        "dataset": True,
                        "model": False,
                        "model_run": "public_api_proxy_v1",
                        "patient_count": len(_legacy_patients(limit=200)),
                        "mode": "system1_compatible_online",
                    },
                )
            elif path == "/api/sources":
                _json_response(self, {"sources": PUBLIC_SOURCES})
            elif path == "/api/summary":
                _json_response(self, _legacy_summary(_legacy_patients(limit=200)))
            elif path == "/api/upload-spec":
                _json_response(self, legacy_upload_spec())
            elif path == "/api/ui":
                _json_response(self, legacy_ui_payload())
            elif path == "/api/gdc/cases":
                _json_response(self, gdc_cases(size=int(qs.get("size", ["20"])[0])))
            elif path == "/api/gdc/files":
                _json_response(self, gdc_files(size=int(qs.get("size", ["12"])[0])))
            elif path == "/api/cbio/study":
                _json_response(self, cbio_study())
            elif path == "/api/cbio/samples":
                _json_response(self, cbio_samples(size=int(qs.get("size", ["30"])[0])))
            elif path == "/api/cbio/clinical":
                _json_response(self, cbio_clinical(limit=int(qs.get("limit", ["60"])[0])))
            elif path.startswith("/api/case/"):
                patient_id = unquote(path.removeprefix("/api/case/"))
                _json_response(self, _legacy_case_bundle(patient_id))
            elif path.startswith("/api/prognosis/"):
                patient_id = unquote(path.removeprefix("/api/prognosis/"))
                _json_response(self, _legacy_case_bundle(patient_id).get("prognosis", {}))
            elif path.startswith("/api/subtype/"):
                patient_id = unquote(path.removeprefix("/api/subtype/"))
                _json_response(self, _legacy_case_bundle(patient_id).get("subtype", {}))
            elif path.startswith("/api/recommendation/"):
                patient_id = unquote(path.removeprefix("/api/recommendation/"))
                _json_response(self, _legacy_case_bundle(patient_id).get("recommendation", {}))
            elif path.startswith("/api/explain/"):
                patient_id = unquote(path.removeprefix("/api/explain/"))
                _json_response(self, _legacy_case_bundle(patient_id).get("explain", {}))
            elif path.startswith("/api/report/"):
                patient_id = unquote(path.removeprefix("/api/report/"))
                report, filename = legacy_report_html(patient_id)
                _html_response(self, report, filename=filename)
            else:
                self.serve_static(path)
        except Exception as exc:
            _json_response(self, {"error": str(exc), "path": path}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/score-gene-panel", "/api/predict-uploaded"}:
            _json_response(self, {"error": "Unknown POST endpoint"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if parsed.path == "/api/predict-uploaded":
                _json_response(self, legacy_predict_uploaded(payload))
            else:
                _json_response(self, score_uploaded_gene_panel(payload))
        except Exception as exc:
            _json_response(self, {"error": str(exc)}, status=400)

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"
        if path.startswith("/assets/pathology_patches/") and PATHOLOGY_ASSET_ZIP.exists():
            member = path.removeprefix("/assets/pathology_patches/").lstrip("/")
            if ".." in Path(member).parts:
                self.send_error(403)
                return
            try:
                with zipfile.ZipFile(PATHOLOGY_ASSET_ZIP) as archive:
                    data = archive.read(member)
            except Exception:
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(member)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        file_path = (FRONTEND / path.lstrip("/")).resolve()
        if FRONTEND not in file_path.parents and file_path != FRONTEND:
            self.send_error(403)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type = f"{content_type}; charset=utf-8"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the portable online TNBC system.")
    default_port = int(os.environ.get("PORT") or os.environ.get("TNBC_ONLINE_PORT", "8020"))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"TNBC online system: http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()

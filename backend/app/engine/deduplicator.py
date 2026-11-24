# -*- coding: utf-8 -*-
"""
通用数据集去重与防稀释核心引擎 (Dataset Deduplicator)
核心能力:
1. 零网络阻塞/零大模型开销: 纯字符串 SHA-256 序列双哈希 + 本地 256 维多粒度字符特征向量
   (优先复用内存中已就绪的 UniversalFAQRetriever._local_model_instance)；
2. 评测集四步去重选优:
   Step 1: 客户输入序列 (U1..Uk) 归一化哈希硬过滤 + 同精确轮次局部老桶双阈值防冲突；
   Step 2a: 命中具体 faq_id 直接成簇；未命中则按 FAQ_OOD / node_id / 精确轮次 k 分入待拆池；
   Step 2b: 待拆池内按标准答案向量余弦相似度 >= theta 自动拆出 M 个独立问题簇；
   Step 3 & 4: 簇内独立上限 K_eval 下以触发轮客户输入向量 Max-Min 选优 + 按需补足。
3. 训练集四步去重防稀释:
   Step 1: 整通客户序列 (U1..Un) 归一化哈希硬过滤 + 同精确总轮次 L 局部老桶双阈值防冲突；
   Step 2a: 融合轨迹签名 (SOP+FAQ / 仅SOP / 仅FAQ命中) 直接成簇；80% 混合对话分入精确轮次 L 待拆池；
   Step 2b: 待拆池内按整通坐席回复均值向量余弦相似度 >= theta 自动拆出 M 个隐式场景簇；
   Step 3 & 4: 簇内独立上限 K_train 下以整通客户发言均值向量 Max-Min 选优 + 按需补足。
4. .dedup_cache.json 原子旁路向量缓存: append 增量合并 O(1) 复用历史向量。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.app.schemas.chat import ChatMessage
from backend.app.schemas.flywheel import DedupConfig

try:
    import numpy as _np  # type: ignore
except Exception:
    _np = None


DEFAULT_HASH_VEC_DIM = 256
_CACHE_IO_LOCK = threading.Lock()


class DatasetDeduplicator:
    """评测集与训练集四步同构去重与防稀释引擎"""

    def __init__(self, hash_dim: int = DEFAULT_HASH_VEC_DIM) -> None:
        self.hash_dim: int = max(32, int(hash_dim))

    @staticmethod
    def _safe_turn_int(val: Any, default: int = 0) -> int:
        if val is None or isinstance(val, bool):
            return default
        if isinstance(val, int):
            return val
        digits: List[str] = re.findall(r"\d+", str(val))
        return int(digits[0]) if digits else default

    @staticmethod
    def _extract_role_content(msg: Any) -> Tuple[str, str]:
        if isinstance(msg, dict):
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "")
            return role, content
        if isinstance(msg, ChatMessage):
            return str(msg.role).strip().lower(), str(msg.content or "")
        role_val: Any = getattr(msg, "role", "")
        content_val: Any = getattr(msg, "content", "")
        return str(role_val or "").strip().lower(), str(content_val or "")

    @staticmethod
    def normalize_text(text: str) -> str:
        """文本归一化：转小写、折叠连续空白、剔除中英文标点，保留汉字与字母数字。"""
        raw_lower: str = (text or "").strip().lower()
        if not raw_lower:
            return ""
        spaced: str = re.sub(r"[^\u4e00-\u9fffa-z0-9]+", " ", raw_lower)
        collapsed: str = re.sub(r"\s+", " ", spaced).strip()
        cleaned: str = re.sub(r"(?<![a-z0-9])\s+|\s+(?![a-z0-9])", "", collapsed)
        if not cleaned:
            return re.sub(r"\s+", " ", raw_lower)
        return cleaned

    @classmethod
    def extract_user_turns(cls, messages: List[Any]) -> List[str]:
        """按顺序提取消息列表中所有 role == 'user' 的发言文本。"""
        turns: List[str] = []
        for m in messages or []:
            role, content = cls._extract_role_content(m)
            if role == "user":
                turns.append(content)
        return turns

    @classmethod
    def compute_user_sequence_hash(cls, messages: List[Any]) -> str:
        """计算归一化客户输入序列 (U1..Uk) 的 SHA-256 十六进制摘要。"""
        user_turns: List[str] = cls.extract_user_turns(messages)
        norm_turns: List[str] = [cls.normalize_text(t) for t in user_turns]
        norm_turns = [t for t in norm_turns if t]
        if not norm_turns:
            fallback_parts: List[str] = []
            for m in messages or []:
                role, content = cls._extract_role_content(m)
                nt: str = cls.normalize_text(content)
                if nt:
                    fallback_parts.append(f"{role}:{nt}")
            joined: str = "\x1f".join(fallback_parts)
        else:
            joined = "\x1f".join(norm_turns)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    @classmethod
    def compute_dialogue_user_hashes(
        cls,
        raw_messages: List[Any],
        remediated_messages: Optional[List[Any]] = None,
    ) -> Tuple[str, Optional[str]]:
        """返回 (原对话客户序列哈希, 修复后对话客户序列哈希)。"""
        raw_hash: str = cls.compute_user_sequence_hash(raw_messages)
        remed_hash: Optional[str] = (
            cls.compute_user_sequence_hash(remediated_messages)
            if remediated_messages
            else None
        )
        return raw_hash, remed_hash

    @staticmethod
    def count_non_system_turns(messages: List[Any]) -> int:
        count: int = 0
        for m in messages or []:
            role, _ = DatasetDeduplicator._extract_role_content(m)
            if role != "system":
                count += 1
        return count

    @staticmethod
    def _extract_multigranular_tokens(text: str) -> List[str]:
        """提取中文 Unigram/Bigram/Trigram 与英文数字词。"""
        t: str = (text or "").strip().lower()
        if not t:
            return []
        tokens: List[str] = []
        alnum_tokens: List[str] = re.findall(r"[a-z0-9_]+", t)
        tokens.extend(alnum_tokens)

        cjk_chars: List[str] = re.findall(r"[\u4e00-\u9fff]", t)
        if cjk_chars:
            tokens.extend(cjk_chars)
            cjk_len: int = len(cjk_chars)
            for i in range(cjk_len - 1):
                tokens.append(cjk_chars[i] + cjk_chars[i + 1])
            for i in range(cjk_len - 2):
                tokens.append(cjk_chars[i] + cjk_chars[i + 1] + cjk_chars[i + 2])

        if not tokens:
            compact: str = re.sub(r"\s+", "", t)
            for i in range(len(compact)):
                tokens.append(compact[i])
                if i + 1 < len(compact):
                    tokens.append(compact[i : i + 2])
        return tokens

    @staticmethod
    def _l2_normalize(vec: List[float]) -> List[float]:
        if not vec:
            return vec
        norm_sq: float = sum(x * x for x in vec)
        if norm_sq <= 0.0:
            return [0.0] * len(vec)
        inv_norm: float = 1.0 / math.sqrt(norm_sq)
        return [x * inv_norm for x in vec]

    def _hash_vector_256(self, text: str) -> List[float]:
        """纯本地确定性多粒度字符特征向量 (1+ln(tf) 缩放 + 双哈希符号投影 + L2 归一化)。"""
        dim: int = self.hash_dim
        tokens: List[str] = self._extract_multigranular_tokens(text)
        if not tokens:
            return [0.0] * dim

        tf_map: Dict[str, int] = {}
        for tok in tokens:
            tf_map[tok] = tf_map.get(tok, 0) + 1

        vec: List[float] = [0.0] * dim
        for tok, tf in tf_map.items():
            weight: float = 1.0 + math.log(float(tf))
            digest: bytes = hashlib.sha256(tok.encode("utf-8")).digest()
            idx: int = int.from_bytes(digest[:8], "little") % dim
            sign: float = 1.0 if (int.from_bytes(digest[8:16], "little") & 1) == 0 else -1.0
            vec[idx] += sign * weight

        return self._l2_normalize(vec)

    @staticmethod
    def _get_loaded_local_encoder() -> Optional[Tuple[Any, Any]]:
        """仅当 UniversalFAQRetriever._local_model_instance 已在内存中就绪时返回 (tokenizer, model)。"""
        faq_mod: Any = sys.modules.get("backend.app.engine.faq_retriever")
        if faq_mod is None:
            return None
        retriever_cls: Any = getattr(faq_mod, "UniversalFAQRetriever", None)
        if retriever_cls is None:
            return None
        tok: Any = getattr(retriever_cls, "_local_tokenizer_instance", None)
        mdl: Any = getattr(retriever_cls, "_local_model_instance", None)
        if tok is None or mdl is None:
            return None
        return tok, mdl

    @staticmethod
    def _encode_with_local_encoder(
        texts: List[str],
        tok: Any,
        mdl: Any,
    ) -> Optional[List[List[float]]]:
        if not texts:
            return []
        try:
            import torch  # type: ignore

            torch_mod: Any = torch
            encoded: Any = tok(
                texts,
                padding=True,
                truncation=True,
                max_length=256,
                return_tensors="pt",
            )
            with torch_mod.no_grad():
                outputs: Any = mdl(**encoded)
                embeddings: Any = outputs[0][:, 0]
                normalized: Any = torch_mod.nn.functional.normalize(embeddings, p=2, dim=1)
                cpu_tensor: Any = normalized.cpu()
                raw_list: Any = cpu_tensor.tolist()
                return [[float(v) for v in row] for row in raw_list]
        except Exception:
            return None

    @classmethod
    def _try_encode_with_loaded_local_model(cls, texts: List[str]) -> Optional[List[List[float]]]:
        """仅当 UniversalFAQRetriever._local_model_instance 已在内存中就绪时复用推理，绝不触发网络下载。"""
        if not texts:
            return []
        pair: Optional[Tuple[Any, Any]] = cls._get_loaded_local_encoder()
        if pair is None:
            return None
        tok, mdl = pair
        return cls._encode_with_local_encoder(texts, tok, mdl)

    def _resolve_active_encoder(self) -> Tuple[str, int, Optional[Tuple[Any, Any]]]:
        """单次流水线快照当前激活编码器标识、输出维度与句柄，杜绝批内跨编码器混维。"""
        pair: Optional[Tuple[Any, Any]] = self._get_loaded_local_encoder()
        if pair is not None:
            tok, mdl = pair
            probe: Optional[List[List[float]]] = self._encode_with_local_encoder(["探测维度"], tok, mdl)
            if probe and probe[0]:
                dim: int = len(probe[0])
                mdl_name: str = str(
                    getattr(mdl, "name_or_path", None)
                    or getattr(getattr(mdl, "config", None), "_name_or_path", None)
                    or type(mdl).__name__
                ).strip()
                return f"local_cpu:{mdl_name}:{dim}", dim, pair
        return f"hash{self.hash_dim}_v1", self.hash_dim, None

    def encode_batch(
        self,
        texts: List[str],
        *,
        local_encoder: Optional[Tuple[Any, Any]] = None,
        use_local_model: bool = True,
    ) -> List[List[float]]:
        """批量编码文本列表为 L2 归一化本地向量。"""
        if not texts:
            return []
        if use_local_model:
            pair: Optional[Tuple[Any, Any]] = local_encoder or self._get_loaded_local_encoder()
            if pair is not None:
                loaded_vecs: Optional[List[List[float]]] = self._encode_with_local_encoder(texts, pair[0], pair[1])
                if loaded_vecs is not None and len(loaded_vecs) == len(texts):
                    return [self._l2_normalize(v) for v in loaded_vecs]
        return [self._hash_vector_256(t) for t in texts]

    def encode_single_text(self, text: str) -> List[float]:
        """编码单句文本为 L2 归一化本地向量。"""
        vecs: List[List[float]] = self.encode_batch([text or ""])
        return vecs[0] if vecs else [0.0] * self.hash_dim

    def _current_vector_dim(self) -> int:
        _, dim, _ = self._resolve_active_encoder()
        return dim

    def encode_role_mean_vector(
        self,
        messages: List[Any],
        role: str,
        *,
        expected_dim: Optional[int] = None,
        local_encoder: Optional[Tuple[Any, Any]] = None,
        use_local_model: bool = True,
    ) -> List[float]:
        """逐轮编码指定角色的发言向量后求算术均值并 L2 归一化。"""
        dim_fallback: int = expected_dim if expected_dim is not None else (
            self._current_vector_dim() if use_local_model else self.hash_dim
        )
        target_role: str = (role or "").strip().lower()
        role_texts: List[str] = []
        for m in messages or []:
            r, c = self._extract_role_content(m)
            if r == target_role and c.strip():
                role_texts.append(c.strip())
        if not role_texts:
            return [0.0] * dim_fallback

        turn_vecs: List[List[float]] = self.encode_batch(
            role_texts,
            local_encoder=local_encoder,
            use_local_model=use_local_model,
        )
        if not turn_vecs:
            return [0.0] * dim_fallback
        dim: int = len(turn_vecs[0])
        mean_vec: List[float] = [0.0] * dim
        for tv in turn_vecs:
            if len(tv) != dim:
                continue
            for j, val in enumerate(tv):
                mean_vec[j] += val
        n_turns: float = float(len(turn_vecs))
        mean_vec = [x / n_turns for x in mean_vec]
        return self._l2_normalize(mean_vec)

    @staticmethod
    def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """计算两个 L2 归一化向量的余弦相似度（优先 numpy，回退纯 Python）。"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        if _np is not None:
            np_mod: Any = _np
            va: Any = np_mod.asarray(vec_a, dtype=np_mod.float64)
            vb: Any = np_mod.asarray(vec_b, dtype=np_mod.float64)
            na: float = float(np_mod.linalg.norm(va))
            nb: float = float(np_mod.linalg.norm(vb))
            if na <= 0.0 or nb <= 0.0:
                return 0.0
            return float(np_mod.dot(va, vb) / (na * nb))
        dot: float = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a: float = math.sqrt(sum(a * a for a in vec_a))
        norm_b: float = math.sqrt(sum(b * b for b in vec_b))
        if norm_a <= 0.0 or norm_b <= 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    @classmethod
    def _batch_cosine_similarities(cls, query_vec: List[float], pool_vecs: List[List[float]]) -> List[float]:
        """计算单向量与候选池矩阵的余弦相似度列表（利用 numpy 矩阵点积加速）。"""
        if not query_vec or not pool_vecs:
            return []
        dim: int = len(query_vec)
        if _np is not None and all(len(v) == dim for v in pool_vecs):
            np_mod: Any = _np
            q_arr: Any = np_mod.asarray(query_vec, dtype=np_mod.float64)
            q_norm: float = float(np_mod.linalg.norm(q_arr))
            if q_norm <= 0.0:
                return [0.0] * len(pool_vecs)
            mat: Any = np_mod.asarray(pool_vecs, dtype=np_mod.float64)
            norms: Any = np_mod.linalg.norm(mat, axis=1)
            dots: Any = mat @ q_arr
            denom: Any = norms * q_norm
            sims: Any = np_mod.where(denom > 0.0, dots / denom, 0.0)
            return [float(x) for x in sims.tolist()]
        return [cls.cosine_similarity(query_vec, pv) for pv in pool_vecs]

    @classmethod
    def _mean_normalized_vector(cls, vecs: List[List[float]], fallback_dim: int) -> List[float]:
        if not vecs:
            return [0.0] * fallback_dim
        dim: int = len(vecs[0]) or fallback_dim
        if _np is not None and all(len(v) == dim for v in vecs):
            np_mod: Any = _np
            mat: Any = np_mod.asarray(vecs, dtype=np_mod.float64)
            mean_arr: Any = mat.mean(axis=0)
            nrm: float = float(np_mod.linalg.norm(mean_arr))
            if nrm <= 0.0:
                return [0.0] * dim
            return [float(x) for x in (mean_arr / nrm).tolist()]
        acc: List[float] = [0.0] * dim
        for v in vecs:
            if len(v) != dim:
                continue
            for j, val in enumerate(v):
                acc[j] += val
        n_cnt: float = float(len(vecs))
        return cls._l2_normalize([x / n_cnt for x in acc])

    @staticmethod
    def get_cache_path(dataset_file_path: str) -> str:
        """根据数据集文件路径派生旁路向量缓存文件路径 (<path>.dedup_cache.json)。"""
        p: str = (dataset_file_path or "").strip()
        if p.endswith(".jsonl"):
            return p[:-6] + ".dedup_cache.json"
        if p.endswith(".json"):
            return p[:-5] + ".dedup_cache.json"
        return p + ".dedup_cache.json"

    @staticmethod
    def _load_cache_entries(cache_path: Optional[str]) -> Dict[str, Dict[str, Any]]:
        if not cache_path or not os.path.exists(cache_path):
            return {}
        with _CACHE_IO_LOCK:
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    raw: Any = json.load(f)
                if isinstance(raw, dict):
                    entries: Any = raw.get("entries", raw)
                    if isinstance(entries, dict):
                        return dict(entries)
            except Exception:
                pass
        return {}

    @staticmethod
    def _save_cache_entries(cache_path: Optional[str], entries: Dict[str, Dict[str, Any]]) -> None:
        if not cache_path:
            return
        parent_dir: str = os.path.dirname(os.path.abspath(cache_path))
        os.makedirs(parent_dir, exist_ok=True)
        payload: Dict[str, Any] = {"version": 1, "entries": entries}
        tmp_path: str = f"{cache_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with _CACHE_IO_LOCK:
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp_path, cache_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

    @staticmethod
    def _row_content_fingerprint(
        row: Dict[str, Any],
        kind: str,
        expected_dim: int,
        encoder_tag: str = "",
    ) -> str:
        meta: Dict[str, Any] = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        msgs: List[Any] = row.get("messages") or []
        norm_msgs: List[Dict[str, str]] = []
        for m in msgs:
            r, c = DatasetDeduplicator._extract_role_content(m)
            norm_msgs.append({"role": r, "content": c})
        sig_payload: Dict[str, Any] = {
            "kind": kind,
            "dim": expected_dim,
            "encoder_tag": encoder_tag or f"dim_{expected_dim}",
            "id": str(row.get("id") or ""),
            "messages": norm_msgs,
            "reference": row.get("reference"),
            "rubric": row.get("rubric"),
            "explicit_cluster_key": meta.get("dedup_cluster_key") or row.get("dedup_cluster_key") or "",
        }
        raw_str: str = json.dumps(sig_payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

    @staticmethod
    def _get_explicit_cluster_key(row: Dict[str, Any]) -> str:
        meta: Any = row.get("metadata")
        if isinstance(meta, dict) and meta.get("dedup_cluster_key"):
            return str(meta["dedup_cluster_key"]).strip()
        if row.get("dedup_cluster_key"):
            return str(row["dedup_cluster_key"]).strip()
        return ""

    @staticmethod
    def _set_explicit_cluster_key(row: Dict[str, Any], cluster_key: str) -> None:
        meta: Any = row.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
            row["metadata"] = meta
        meta["dedup_cluster_key"] = cluster_key

    @staticmethod
    def _extract_benchmark_answer_text(row: Dict[str, Any]) -> str:
        ref: Any = row.get("reference")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
        rubric: Any = row.get("rubric")
        if isinstance(rubric, str) and rubric.strip():
            return rubric.strip()
        if isinstance(rubric, dict):
            jc: Any = rubric.get("judge_criteria")
            if isinstance(jc, str) and jc.strip():
                return jc.strip()
            return json.dumps(rubric, sort_keys=True, ensure_ascii=False)
        user_turns: List[str] = DatasetDeduplicator.extract_user_turns(row.get("messages") or [])
        return user_turns[-1].strip() if user_turns else ""

    @staticmethod
    def _extract_last_user_text(messages: List[Any]) -> str:
        user_turns: List[str] = DatasetDeduplicator.extract_user_turns(messages)
        if user_turns:
            return user_turns[-1].strip()
        for m in reversed(messages or []):
            _, c = DatasetDeduplicator._extract_role_content(m)
            if c.strip():
                return c.strip()
        return ""

    def _compute_row_features(
        self,
        row: Dict[str, Any],
        kind: str,
        cache_entries: Dict[str, Dict[str, Any]],
        expected_dim: int,
        encoder_tag: str = "",
        local_encoder: Optional[Tuple[Any, Any]] = None,
        use_local_model: bool = True,
    ) -> Tuple[Dict[str, Any], bool]:
        """计算或从缓存复用单行数据的哈希、精确轮次、客户向量与回复/答案向量。"""
        fp: str = self._row_content_fingerprint(
            row, kind=kind, expected_dim=expected_dim, encoder_tag=encoder_tag
        )
        cached: Optional[Dict[str, Any]] = cache_entries.get(fp)
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("user_vec"), list)
            and isinstance(cached.get("reply_vec"), list)
            and len(cached["user_vec"]) == expected_dim
            and len(cached["reply_vec"]) == expected_dim
            and cached.get("user_seq_hash")
        ):
            return dict(cached, fp=fp), True

        msgs: List[Any] = row.get("messages") or []
        user_seq_hash: str = self.compute_user_sequence_hash(msgs)
        exact_turn: int = self.count_non_system_turns(msgs)
        explicit_key: str = self._get_explicit_cluster_key(row)

        if kind == "benchmark":
            last_u: str = self._extract_last_user_text(msgs)
            ans_txt: str = self._extract_benchmark_answer_text(row)
            encoded: List[List[float]] = self.encode_batch(
                [last_u, ans_txt],
                local_encoder=local_encoder,
                use_local_model=use_local_model,
            )
            user_vec: List[float] = encoded[0]
            reply_vec: List[float] = encoded[1]
        else:
            user_vec = self.encode_role_mean_vector(
                msgs,
                "user",
                expected_dim=expected_dim,
                local_encoder=local_encoder,
                use_local_model=use_local_model,
            )
            reply_vec = self.encode_role_mean_vector(
                msgs,
                "assistant",
                expected_dim=expected_dim,
                local_encoder=local_encoder,
                use_local_model=use_local_model,
            )

        feat: Dict[str, Any] = {
            "fp": fp,
            "id": str(row.get("id") or ""),
            "user_seq_hash": user_seq_hash,
            "exact_turn": exact_turn,
            "explicit_cluster_key": explicit_key,
            "user_vec": user_vec,
            "reply_vec": reply_vec,
        }
        return feat, False

    def _prepare_existing_index(
        self,
        existing_rows: Optional[List[Dict[str, Any]]],
        kind: str,
        cache_entries: Dict[str, Dict[str, Any]],
        expected_dim: int,
        encoder_tag: str = "",
        local_encoder: Optional[Tuple[Any, Any]] = None,
        use_local_model: bool = True,
    ) -> Tuple[
        List[Dict[str, Any]],
        List[Dict[str, Any]],
        Dict[str, List[int]],
        Dict[str, int],
        Dict[str, List[Tuple[int, List[float], List[float]]]],
        Dict[int, List[Tuple[int, List[float], List[float]]]],
        int,
        int,
    ]:
        """构建已有老数据的哈希表、已标记簇基底索引与无标签精确轮次局部桶索引。"""
        merged_existing: List[Dict[str, Any]] = [dict(r) for r in (existing_rows or []) if isinstance(r, dict)]
        existing_feats: List[Dict[str, Any]] = []
        h_old: Dict[str, List[int]] = {}
        old_id_to_idx: Dict[str, int] = {}
        labeled_clusters: Dict[str, List[Tuple[int, List[float], List[float]]]] = {}
        unlabeled_turn_buckets: Dict[int, List[Tuple[int, List[float], List[float]]]] = {}
        hits: int = 0
        misses: int = 0

        for idx, row in enumerate(merged_existing):
            feat, is_hit = self._compute_row_features(
                row,
                kind=kind,
                cache_entries=cache_entries,
                expected_dim=expected_dim,
                encoder_tag=encoder_tag,
                local_encoder=local_encoder,
                use_local_model=use_local_model,
            )
            if is_hit:
                hits += 1
            else:
                misses += 1
            existing_feats.append(feat)
            h_val: str = str(feat["user_seq_hash"])
            h_old.setdefault(h_val, []).append(idx)
            rid: str = str(row.get("id") or "")
            if rid:
                old_id_to_idx[rid] = idx

            ckey: str = str(feat.get("explicit_cluster_key") or "")
            if ckey:
                labeled_clusters.setdefault(ckey, []).append(
                    (idx, feat["user_vec"], feat["reply_vec"])
                )
            else:
                turn_k: int = int(feat.get("exact_turn", 0))
                unlabeled_turn_buckets.setdefault(turn_k, []).append(
                    (idx, feat["user_vec"], feat["reply_vec"])
                )

        return (
            merged_existing,
            existing_feats,
            h_old,
            old_id_to_idx,
            labeled_clusters,
            unlabeled_turn_buckets,
            hits,
            misses,
        )

    def _split_unsplit_pool(
        self,
        pool_key: str,
        pool_items: List[Dict[str, Any]],
        sub_prefix: str,
        sim_threshold: float,
        labeled_clusters: Dict[str, List[Tuple[int, List[float], List[float]]]],
        expected_dim: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Step 2b: 在未细分待拆池内按坐席答案/回复向量增量中心聚类成 M 个独立簇。"""
        centroids: List[Tuple[str, List[float], int]] = []
        prefix_match: str = f"{pool_key}::{sub_prefix}_"
        max_existing_idx: int = -1

        for ckey in sorted(labeled_clusters.keys()):
            if ckey.startswith(prefix_match):
                bases = labeled_clusters[ckey]
                reply_vecs: List[List[float]] = [b[2] for b in bases]
                c_vec: List[float] = self._mean_normalized_vector(reply_vecs, expected_dim)
                centroids.append((ckey, c_vec, len(bases)))
                suffix_str: str = ckey[len(prefix_match) :]
                if suffix_str.isdigit():
                    max_existing_idx = max(max_existing_idx, int(suffix_str))

        next_sub_idx: int = max_existing_idx + 1
        out_clusters: Dict[str, List[Dict[str, Any]]] = {}

        for item in pool_items:
            r_vec: List[float] = item["feat"]["reply_vec"]
            best_pos: int = -1
            best_sim: float = -1.0
            if centroids:
                c_vecs: List[List[float]] = [c[1] for c in centroids]
                sims: List[float] = self._batch_cosine_similarities(r_vec, c_vecs)
                for pos, sim_val in enumerate(sims):
                    if sim_val > best_sim:
                        best_sim = sim_val
                        best_pos = pos

            if best_pos >= 0 and best_sim >= sim_threshold:
                ckey, old_cent, cnt = centroids[best_pos]
                new_cnt: int = cnt + 1
                dim: int = len(old_cent)
                blended: List[float] = [
                    (old_cent[j] * float(cnt) + r_vec[j]) / float(new_cnt)
                    for j in range(dim)
                ]
                centroids[best_pos] = (ckey, self._l2_normalize(blended), new_cnt)
            else:
                ckey = f"{pool_key}::{sub_prefix}_{next_sub_idx}"
                next_sub_idx += 1
                centroids.append((ckey, r_vec, 1))

            out_clusters.setdefault(ckey, []).append(item)

        return out_clusters

    def _max_min_select_cluster(
        self,
        items: List[Dict[str, Any]],
        quota: int,
        old_base_user_vecs: List[List[float]],
        expected_dim: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Step 3: 在单个簇内基于客户输入向量执行 Max-Min 最远点选优。"""
        rem: int = max(0, quota - len(old_base_user_vecs))
        if rem == 0:
            return [], list(items)
        if len(items) <= rem:
            return list(items), []

        selected_vecs: List[List[float]] = list(old_base_user_vecs)
        remaining_indices: List[int] = list(range(len(items)))
        chosen_indices: List[int] = []

        # 无老基底时先选离簇均值中心最近的 1 条作典型种子
        if not selected_vecs:
            all_u_vecs: List[List[float]] = [items[i]["feat"]["user_vec"] for i in remaining_indices]
            centroid: List[float] = self._mean_normalized_vector(all_u_vecs, expected_dim)
            sims_to_center: List[float] = self._batch_cosine_similarities(centroid, all_u_vecs)
            seed_pos: int = max(range(len(remaining_indices)), key=lambda idx: sims_to_center[idx])
            seed_item_idx: int = remaining_indices.pop(seed_pos)
            chosen_indices.append(seed_item_idx)
            selected_vecs.append(items[seed_item_idx]["feat"]["user_vec"])

        while len(chosen_indices) < rem and remaining_indices:
            cand_vecs: List[List[float]] = [items[i]["feat"]["user_vec"] for i in remaining_indices]
            max_sims: List[float] = [-2.0] * len(remaining_indices)
            for s_vec in selected_vecs:
                s_sims: List[float] = self._batch_cosine_similarities(s_vec, cand_vecs)
                for p, sv in enumerate(s_sims):
                    if sv > max_sims[p]:
                        max_sims[p] = sv
            best_pos: int = min(range(len(remaining_indices)), key=lambda p: max_sims[p])
            picked_idx: int = remaining_indices.pop(best_pos)
            chosen_indices.append(picked_idx)
            selected_vecs.append(items[picked_idx]["feat"]["user_vec"])

        chosen_set: Set[int] = set(chosen_indices)
        selected_items: List[Dict[str, Any]] = [items[i] for i in chosen_indices]
        overflow_items: List[Dict[str, Any]] = [
            items[i] for i in range(len(items)) if i not in chosen_set
        ]
        return selected_items, overflow_items

    def _backfill_from_overflow(
        self,
        overflow_pool: List[Dict[str, Any]],
        all_selected_vecs: List[List[float]],
        needed: int,
        old_id_to_idx: Optional[Dict[str, int]] = None,
        replaced_old_idxs: Optional[Set[int]] = None,
        net_new_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Step 4: 若设了目标总条数且未满，从余量池按客户输入距离最远依次补足（净增行数达标即止）。"""
        if needed <= 0 or not overflow_pool:
            return []
        pool: List[Dict[str, Any]] = list(overflow_pool)
        ref_vecs: List[List[float]] = list(all_selected_vecs)
        backfilled: List[Dict[str, Any]] = []
        id_map: Dict[str, int] = old_id_to_idx or {}
        seen_replaced: Set[int] = set(replaced_old_idxs or set())
        seen_new: Set[str] = set(net_new_ids or set())
        net_added: int = 0

        while net_added < needed and pool:
            if not ref_vecs:
                picked: Dict[str, Any] = pool.pop(0)
            else:
                cand_vecs: List[List[float]] = [it["feat"]["user_vec"] for it in pool]
                max_sims: List[float] = [-2.0] * len(pool)
                for rv in ref_vecs:
                    r_sims: List[float] = self._batch_cosine_similarities(rv, cand_vecs)
                    for p, sv in enumerate(r_sims):
                        if sv > max_sims[p]:
                            max_sims[p] = sv
                best_pos: int = min(range(len(pool)), key=lambda p: max_sims[p])
                picked = pool.pop(best_pos)

            backfilled.append(picked)
            ref_vecs.append(picked["feat"]["user_vec"])
            rid: str = str(picked["row"].get("id") or "")
            if rid and rid in id_map:
                seen_replaced.add(id_map[rid])
            elif rid:
                if rid not in seen_new:
                    seen_new.add(rid)
                    net_added += 1
            else:
                net_added += 1

        return backfilled

    @staticmethod
    def _merge_passthrough(
        candidate_rows: List[Dict[str, Any]],
        existing_rows: Optional[List[Dict[str, Any]]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        id_to_pos: Dict[str, int] = {}
        for row in existing_rows or []:
            if not isinstance(row, dict):
                continue
            copied: Dict[str, Any] = dict(row)
            copied.pop("_dedup_meta", None)
            rid: str = str(copied.get("id") or "")
            if rid and rid in id_to_pos:
                merged[id_to_pos[rid]] = copied
            else:
                if rid:
                    id_to_pos[rid] = len(merged)
                merged.append(copied)

        kept_cands: int = 0
        for row in candidate_rows:
            if not isinstance(row, dict):
                continue
            copied = dict(row)
            copied.pop("_dedup_meta", None)
            rid = str(copied.get("id") or "")
            if rid and rid in id_to_pos:
                merged[id_to_pos[rid]] = copied
            else:
                if rid:
                    id_to_pos[rid] = len(merged)
                merged.append(copied)
            kept_cands += 1

        stats: Dict[str, Any] = {
            "enabled": False,
            "input_candidates": len(candidate_rows),
            "existing_count": len(existing_rows or []),
            "exact_hash_dropped": 0,
            "near_dup_dropped": 0,
            "replaced_existing_count": 0,
            "cluster_count": 0,
            "explicit_cluster_count": 0,
            "implicit_cluster_count": 0,
            "quota_dropped": 0,
            "backfilled_count": 0,
            "kept_candidates": kept_cands,
            "final_total_count": len(merged),
        }
        return merged, stats

    def _run_four_step_pipeline(
        self,
        *,
        kind: str,
        candidate_rows: List[Dict[str, Any]],
        existing_rows: Optional[List[Dict[str, Any]]],
        config: Optional[DedupConfig],
        cache_path: Optional[str],
        default_quota: int,
        sub_prefix: str,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        eff_cfg: DedupConfig = config or DedupConfig()
        if not eff_cfg.enabled:
            return self._merge_passthrough(candidate_rows, existing_rows)

        quota: int = (
            int(eff_cfg.cluster_quota)
            if eff_cfg.cluster_quota and eff_cfg.cluster_quota > 0
            else default_quota
        )
        ans_thresh: float = float(eff_cfg.answer_sim_threshold)
        near_dup_thresh: float = float(eff_cfg.near_dup_input_threshold)
        target_total: Optional[int] = (
            int(eff_cfg.target_total_count)
            if eff_cfg.target_total_count and eff_cfg.target_total_count > 0
            else None
        )

        encoder_tag, expected_dim, local_enc_snap = self._resolve_active_encoder()
        use_local_snap: bool = local_enc_snap is not None
        cache_entries: Dict[str, Dict[str, Any]] = self._load_cache_entries(cache_path)

        (
            merged_existing,
            existing_feats,
            h_old,
            old_id_to_idx,
            labeled_clusters,
            unlabeled_turn_buckets,
            cache_hits,
            cache_misses,
        ) = self._prepare_existing_index(
            existing_rows=existing_rows,
            kind=kind,
            cache_entries=cache_entries,
            expected_dim=expected_dim,
            encoder_tag=encoder_tag,
            local_encoder=local_enc_snap,
            use_local_model=use_local_snap,
        )

        # Step 1: 客户输入序列哈希硬过滤 + 同精确轮次局部老桶双阈值防冲突
        seen_cand_hashes: Set[str] = set()
        exact_hash_dropped: int = 0
        near_dup_dropped: int = 0
        replaced_existing_count: int = 0
        surviving_items: List[Dict[str, Any]] = []

        for orig_pos, raw_row in enumerate(candidate_rows):
            row: Dict[str, Any] = dict(raw_row)
            dedup_meta: Dict[str, Any] = (
                dict(row.pop("_dedup_meta"))
                if isinstance(row.get("_dedup_meta"), dict)
                else {}
            )
            cand_id: str = str(row.get("id") or "")
            feat, is_hit = self._compute_row_features(
                row,
                kind=kind,
                cache_entries=cache_entries,
                expected_dim=expected_dim,
                encoder_tag=encoder_tag,
                local_encoder=local_enc_snap,
                use_local_model=use_local_snap,
            )
            if is_hit:
                cache_hits += 1
            else:
                cache_misses += 1

            h_val: str = str(feat["user_seq_hash"])

            # 1a. 命中老库哈希：同 ID 原地替换刷新，否则跳过
            if h_val in h_old:
                matched_old_idxs: List[int] = h_old[h_val]
                same_id_idx: Optional[int] = next(
                    (
                        oi
                        for oi in matched_old_idxs
                        if cand_id and str(merged_existing[oi].get("id") or "") == cand_id
                    ),
                    None,
                )
                if same_id_idx is not None:
                    prev_ckey: str = self._get_explicit_cluster_key(merged_existing[same_id_idx])
                    if prev_ckey and not self._get_explicit_cluster_key(row):
                        self._set_explicit_cluster_key(row, prev_ckey)
                    merged_existing[same_id_idx] = row
                    existing_feats[same_id_idx] = feat
                    replaced_existing_count += 1
                else:
                    exact_hash_dropped += 1
                continue

            # 1b. 本批次内重复客户序列哈希过滤
            if h_val in seen_cand_hashes:
                exact_hash_dropped += 1
                continue
            seen_cand_hashes.add(h_val)

            # 1c. 外部无标签老数据：仅在同精确轮次局部桶内计算余弦相似度
            turn_k: int = int(feat.get("exact_turn", 0))
            local_bucket: List[Tuple[int, List[float], List[float]]] = unlabeled_turn_buckets.get(turn_k, [])
            claimed_old_idxs: Set[int] = set()
            is_near_dup: bool = False

            if local_bucket:
                b_reply_vecs: List[List[float]] = [b[2] for b in local_bucket]
                b_user_vecs: List[List[float]] = [b[1] for b in local_bucket]
                sim_ans_list: List[float] = self._batch_cosine_similarities(feat["reply_vec"], b_reply_vecs)
                sim_user_list: List[float] = self._batch_cosine_similarities(feat["user_vec"], b_user_vecs)

                for b_pos, (old_idx, _, _) in enumerate(local_bucket):
                    sim_ans: float = sim_ans_list[b_pos]
                    sim_user: float = sim_user_list[b_pos]
                    if sim_ans >= ans_thresh and sim_user >= near_dup_thresh:
                        old_id_str: str = str(merged_existing[old_idx].get("id") or "")
                        if cand_id and old_id_str == cand_id:
                            merged_existing[old_idx] = row
                            existing_feats[old_idx] = feat
                            replaced_existing_count += 1
                        else:
                            near_dup_dropped += 1
                        is_near_dup = True
                        break
                    if sim_ans >= ans_thresh:
                        claimed_old_idxs.add(old_idx)

            if is_near_dup:
                continue

            surviving_items.append({
                "orig_pos": orig_pos,
                "row": row,
                "feat": feat,
                "dedup_meta": dedup_meta,
                "claimed_old_idxs": claimed_old_idxs,
            })

        # Step 2a: 路由至显式簇或待拆池
        explicit_clusters: Dict[str, List[Dict[str, Any]]] = {}
        unsplit_pools: Dict[str, List[Dict[str, Any]]] = {}

        for item in surviving_items:
            if kind == "benchmark":
                explicit_key, pool_key = self._route_benchmark_cluster(item["row"], item["feat"])
            else:
                explicit_key, pool_key = self._route_training_cluster(
                    item["row"], item["feat"], item["dedup_meta"]
                )
            if explicit_key:
                explicit_clusters.setdefault(explicit_key, []).append(item)
            elif pool_key:
                unsplit_pools.setdefault(pool_key, []).append(item)

        # Step 2b: 待拆池内按坐席回复/标准答案向量自动拆出 M 个隐式场景簇
        implicit_clusters: Dict[str, List[Dict[str, Any]]] = {}
        for pool_key, pool_items in unsplit_pools.items():
            sub_map: Dict[str, List[Dict[str, Any]]] = self._split_unsplit_pool(
                pool_key=pool_key,
                pool_items=pool_items,
                sub_prefix=sub_prefix,
                sim_threshold=ans_thresh,
                labeled_clusters=labeled_clusters,
                expected_dim=expected_dim,
            )
            implicit_clusters.update(sub_map)

        all_clusters: Dict[str, List[Dict[str, Any]]] = {}
        all_clusters.update(explicit_clusters)
        all_clusters.update(implicit_clusters)

        # Step 3: 每簇独立上限 K 下 Max-Min 选优
        selected_items: List[Dict[str, Any]] = []
        overflow_pool: List[Dict[str, Any]] = []

        for ckey, c_items in all_clusters.items():
            base_indices: Set[int] = {b[0] for b in labeled_clusters.get(ckey, [])}
            for it in c_items:
                base_indices.update(it["claimed_old_idxs"])
            # 若候选中存在与老库同 ID 的替换样本（不同客户序列且非近重复），预扣除将被替换老行名额
            replacing_old_idxs: Set[int] = {
                old_id_to_idx[str(it["row"].get("id") or "")]
                for it in c_items
                if str(it["row"].get("id") or "") in old_id_to_idx
            }
            eff_base_indices: Set[int] = base_indices - replacing_old_idxs
            old_base_u_vecs: List[List[float]] = [
                existing_feats[oi]["user_vec"] for oi in sorted(eff_base_indices)
            ]
            chosen, overflow = self._max_min_select_cluster(
                items=c_items,
                quota=quota,
                old_base_user_vecs=old_base_u_vecs,
                expected_dim=expected_dim,
            )
            for it in chosen:
                it["cluster_key"] = ckey
                selected_items.append(it)
            for it in overflow:
                it["cluster_key"] = ckey
                overflow_pool.append(it)

        # Step 4: 若设了目标总条数且未满，从余量池按客户输入距离最远依次补足（预扣同 ID 原地替换槽位）
        backfilled_count: int = 0
        replaced_by_selected: Set[int] = {
            old_id_to_idx[str(it["row"].get("id") or "")]
            for it in selected_items
            if str(it["row"].get("id") or "") in old_id_to_idx
        }
        net_new_ids: Set[str] = set()
        net_new_count: int = 0
        for it in selected_items:
            rid_sel: str = str(it["row"].get("id") or "")
            if rid_sel and rid_sel in old_id_to_idx:
                continue
            if rid_sel:
                if rid_sel in net_new_ids:
                    continue
                net_new_ids.add(rid_sel)
            net_new_count += 1

        current_total: int = len(merged_existing) + net_new_count
        if target_total is not None and current_total < target_total and overflow_pool:
            all_sel_vecs: List[List[float]] = [
                f["user_vec"]
                for i, f in enumerate(existing_feats)
                if i not in replaced_by_selected
            ] + [it["feat"]["user_vec"] for it in selected_items]
            needed: int = target_total - current_total
            extra_items: List[Dict[str, Any]] = self._backfill_from_overflow(
                overflow_pool=overflow_pool,
                all_selected_vecs=all_sel_vecs,
                needed=needed,
                old_id_to_idx=old_id_to_idx,
                replaced_old_idxs=replaced_by_selected,
                net_new_ids=net_new_ids,
            )
            backfilled_count = len(extra_items)
            selected_items.extend(extra_items)

        # 保持相对原顺序并写回簇签名
        selected_items.sort(key=lambda it: int(it["orig_pos"]))
        kept_candidates_rows: List[Dict[str, Any]] = []
        kept_new_id_to_pos: Dict[str, int] = {}
        for it in selected_items:
            row_out: Dict[str, Any] = it["row"]
            self._set_explicit_cluster_key(row_out, str(it["cluster_key"]))
            rid_out: str = str(row_out.get("id") or "")
            if rid_out and rid_out in old_id_to_idx:
                target_idx: int = old_id_to_idx[rid_out]
                merged_existing[target_idx] = row_out
                existing_feats[target_idx] = it["feat"]
                replaced_existing_count += 1
            elif rid_out and rid_out in kept_new_id_to_pos:
                kept_candidates_rows[kept_new_id_to_pos[rid_out]] = row_out
            else:
                if rid_out:
                    kept_new_id_to_pos[rid_out] = len(kept_candidates_rows)
                kept_candidates_rows.append(row_out)

        final_rows: List[Dict[str, Any]] = merged_existing + kept_candidates_rows

        # 刷新旁路向量缓存
        if cache_path:
            new_cache: Dict[str, Dict[str, Any]] = {}
            for r_obj, f_obj in zip(merged_existing, existing_feats):
                fp_m: str = self._row_content_fingerprint(
                    r_obj, kind=kind, expected_dim=expected_dim, encoder_tag=encoder_tag
                )
                new_cache[fp_m] = {
                    "id": str(r_obj.get("id") or ""),
                    "user_seq_hash": f_obj["user_seq_hash"],
                    "exact_turn": f_obj["exact_turn"],
                    "explicit_cluster_key": self._get_explicit_cluster_key(r_obj),
                    "user_vec": f_obj["user_vec"],
                    "reply_vec": f_obj["reply_vec"],
                }
            for it in selected_items:
                r_obj = it["row"]
                f_obj = it["feat"]
                fp_k: str = self._row_content_fingerprint(
                    r_obj, kind=kind, expected_dim=expected_dim, encoder_tag=encoder_tag
                )
                new_cache[fp_k] = {
                    "id": str(r_obj.get("id") or ""),
                    "user_seq_hash": f_obj["user_seq_hash"],
                    "exact_turn": f_obj["exact_turn"],
                    "explicit_cluster_key": self._get_explicit_cluster_key(r_obj),
                    "user_vec": f_obj["user_vec"],
                    "reply_vec": f_obj["reply_vec"],
                }
            self._save_cache_entries(cache_path, new_cache)

        stats: Dict[str, Any] = {
            "enabled": True,
            "input_candidates": len(candidate_rows),
            "existing_count": len(existing_rows or []),
            "exact_hash_dropped": exact_hash_dropped,
            "near_dup_dropped": near_dup_dropped,
            "replaced_existing_count": replaced_existing_count,
            "cluster_count": len(all_clusters),
            "explicit_cluster_count": len(explicit_clusters),
            "implicit_cluster_count": len(implicit_clusters),
            "quota_dropped": max(0, len(overflow_pool) - backfilled_count),
            "backfilled_count": backfilled_count,
            "kept_candidates": len(kept_candidates_rows),
            "final_total_count": len(final_rows),
            "cluster_quota": quota,
            "answer_sim_threshold": ans_thresh,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
        }
        return final_rows, stats

    @staticmethod
    def _route_benchmark_cluster(row: Dict[str, Any], feat: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        meta: Dict[str, Any] = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        rule_id: str = str(meta.get("rule_id") or row.get("rule_id") or "").strip()
        node_id: str = str(meta.get("node_id") or "").strip()
        faq_id: str = str(meta.get("faq_id") or "").strip()
        is_faq_ood: bool = bool(meta.get("is_faq_ood", False)) or faq_id == "FAQ_OOD"
        exact_k: int = int(feat.get("exact_turn", 0))

        if faq_id and not is_faq_ood and faq_id not in ("NONE", "NULL"):
            return f"{rule_id}::FAQ::{faq_id}", None
        if is_faq_ood:
            if node_id:
                return None, f"{rule_id}::FAQ_OOD::{node_id}"
            return None, f"{rule_id}::FAQ_OOD_TURN::{exact_k}"
        if node_id:
            return None, f"{rule_id}::NODE::{node_id}"
        return None, f"{rule_id}::TURN::{exact_k}"

    @classmethod
    def build_dialogue_trajectory_signature(
        cls,
        rule_id: str,
        scenario_id: Optional[str],
        trajectory_details: Optional[List[Dict[str, Any]]] = None,
        faq_details: Optional[List[Dict[str, Any]]] = None,
        node_trajectory: Optional[List[str]] = None,
    ) -> Tuple[str, Optional[str]]:
        """
        构建训练集融合轨迹签名:
        - SOP 开 + FAQ 开: s1->s2[FAQ_001]->s3
        - 仅 SOP 开: s1->s2->s3
        - 仅 FAQ 开且触发至少 1 个有效 faq_id: FAQ_SEQ:[FAQ_001->FAQ_005]
        - 其余混合对话 (80% 未触发 FAQ / 双开关全关 / 外部无标签导入): 返回 None
        """
        _ = rule_id
        sid: str = str(scenario_id or "").strip()
        if not sid or sid in ("default", "manual_playground") or sid.startswith("row_"):
            persona_key: str = ""
        else:
            persona_key = sid

        valid_faqs: List[Tuple[int, int, str]] = []
        for idx, fd in enumerate(faq_details or []):
            if not isinstance(fd, dict):
                continue
            fid: str = str(fd.get("matched_faq_id") or "").strip()
            is_ood: bool = bool(fd.get("is_ood", False)) or fid == "FAQ_OOD"
            if not fid or is_ood or fid in ("NONE", "NULL"):
                continue
            a_turn: int = cls._safe_turn_int(fd.get("assistant_turn") or fd.get("user_turn"), 0)
            valid_faqs.append((a_turn, idx, fid))
        valid_faqs.sort(key=lambda x: (x[0], x[1]))

        traj_entries: List[Tuple[int, int, str, List[str]]] = []
        for idx, td in enumerate(trajectory_details or []):
            if not isinstance(td, dict):
                continue
            nid: str = str(td.get("node_id") or "").strip()
            if not nid:
                continue
            t_num: int = cls._safe_turn_int(td.get("turn"), idx + 1)
            traj_entries.append((t_num, idx, nid, []))
        traj_entries.sort(key=lambda x: (x[0], x[1]))

        # 兼容只有 node_trajectory 无 trajectory_details 的历史报告
        if not traj_entries and node_trajectory:
            for idx, raw_nid in enumerate(node_trajectory):
                nid = str(raw_nid or "").strip()
                if nid:
                    traj_entries.append((idx + 1, idx, nid, []))

        if traj_entries:
            # 将每个有效 faq_id 挂载到对应或最近前置轮次节点上
            for a_turn, _, fid in valid_faqs:
                target_pos: int = 0
                for pos, (t_num, _, _, _) in enumerate(traj_entries):
                    if t_num <= a_turn:
                        target_pos = pos
                    else:
                        break
                if fid not in traj_entries[target_pos][3]:
                    traj_entries[target_pos][3].append(fid)

            # 合并连续相同 node_id 的自旋（去重保序汇总该段内的 faq_id）
            segments: List[Tuple[str, List[str]]] = []
            for _, _, nid, node_faqs in traj_entries:
                if segments and segments[-1][0] == nid:
                    for fid in node_faqs:
                        if fid not in segments[-1][1]:
                            segments[-1][1].append(fid)
                else:
                    segments.append((nid, list(node_faqs)))

            parts: List[str] = []
            for nid, seg_faqs in segments:
                if seg_faqs:
                    parts.append(f"{nid}[{','.join(seg_faqs)}]")
                else:
                    parts.append(nid)
            return persona_key, "->".join(parts)

        if valid_faqs:
            seq: str = "->".join(fid for _, _, fid in valid_faqs)
            return persona_key, f"FAQ_SEQ:[{seq}]"

        return persona_key, None

    def _route_training_cluster(
        self,
        row: Dict[str, Any],
        feat: Dict[str, Any],
        dedup_meta: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str]]:
        meta: Dict[str, Any] = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        rule_id: str = str(meta.get("rule_id") or row.get("rule_id") or "").strip()
        scenario_id: str = str(meta.get("scenario_id") or row.get("scenario_id") or "").strip()

        traj_details: Any = dedup_meta.get("trajectory_details")
        if not isinstance(traj_details, list):
            traj_details = meta.get("trajectory_details") if isinstance(meta.get("trajectory_details"), list) else []

        faq_details: Any = dedup_meta.get("faq_details")
        if not isinstance(faq_details, list):
            faq_details = meta.get("faq_details") if isinstance(meta.get("faq_details"), list) else []

        node_traj: Any = dedup_meta.get("node_trajectory")
        if not isinstance(node_traj, list):
            node_traj = meta.get("node_trajectory") if isinstance(meta.get("node_trajectory"), list) else []

        persona_key, sig = self.build_dialogue_trajectory_signature(
            rule_id=rule_id,
            scenario_id=scenario_id,
            trajectory_details=traj_details,
            faq_details=faq_details,
            node_trajectory=node_traj,
        )
        if sig:
            return f"{rule_id}::{persona_key}::TRAJ::{sig}", None
        exact_len: int = int(feat.get("exact_turn", 0))
        return None, f"{rule_id}::{persona_key}::TURN::{exact_len}"

    def deduplicate_benchmark_rows(
        self,
        candidate_rows: List[Dict[str, Any]],
        existing_rows: Optional[List[Dict[str, Any]]] = None,
        config: Optional[DedupConfig] = None,
        cache_path: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """评测集生成 / 追加四步去重与防稀释。"""
        return self._run_four_step_pipeline(
            kind="benchmark",
            candidate_rows=candidate_rows,
            existing_rows=existing_rows,
            config=config,
            cache_path=cache_path,
            default_quota=5,
            sub_prefix="ANS",
        )

    def deduplicate_training_rows(
        self,
        candidate_rows: List[Dict[str, Any]],
        existing_rows: Optional[List[Dict[str, Any]]] = None,
        config: Optional[DedupConfig] = None,
        cache_path: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """训练集生成 / 追加四步去重与防稀释。"""
        return self._run_four_step_pipeline(
            kind="training",
            candidate_rows=candidate_rows,
            existing_rows=existing_rows,
            config=config,
            cache_path=cache_path,
            default_quota=20,
            sub_prefix="REPLY",
        )

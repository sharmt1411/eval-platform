# -*- coding: utf-8 -*-
"""
通用 FAQ 检索引擎 (Universal FAQ Retriever)
支持:
1. 高性能 BM25 / N-gram 词法精准检索 (零外部依赖，专有名词/数字/工号 100% 敏锐)
2. 稠密向量检索 (Dense Embedding): 支持本地轻量 CPU 模型与远程兼容 API 嵌入
3. 混合检索与倒数排名融合 (Hybrid RRF Fusion)
4. 知识库向量在内存中预计算与按需缓存 (零冗余计算)
"""
import math
import re
import hashlib
from typing import List, Dict, Any, Optional, Tuple
from backend.app.schemas.rule import FAQItem, FAQInspectionConfig, EmbeddingConfig


def tokenize_chinese_lexical(text: str) -> List[str]:
    """
    轻量分词器 (零第三方依赖):
    支持提取连续英文数字单词、以及中文字符 Unigram 与相邻 Bigram，
    在中文专业术语与短句检索中具备出色的召回率与抗切分错误能力。
    """
    if not text:
        return []
    text = text.lower().strip()
    tokens = []
    
    # 提取英文/数字词
    alnum_tokens = re.findall(r"[a-z0-9_]+", text)
    tokens.extend(alnum_tokens)
    
    # 清洗标点，提取中文字符串
    chinese_chars = re.findall(r"[\u4e00-\u9fa5]", text)
    if chinese_chars:
        # Unigrams
        tokens.extend(chinese_chars)
        # Bigrams
        for i in range(len(chinese_chars) - 1):
            tokens.append(chinese_chars[i] + chinese_chars[i + 1])
            
    return tokens


class BM25Retriever:
    """标准 BM25 词法检索实现"""

    def __init__(self, items: List[FAQItem], k1: float = 1.5, b: float = 0.75):
        self.items = items
        self.k1 = k1
        self.b = b
        self.corpus_size = len(items)
        self.doc_len: List[int] = []
        self.avg_doc_len = 0.0
        self.doc_freqs: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self.doc_tokens: List[List[str]] = []
        self._build_index()

    def _build_index(self):
        total_len = 0
        for item in self.items:
            # question 赋予双倍检索权重，answer 补充细节
            q_tokens = tokenize_chinese_lexical(item.question) * 2
            a_tokens = tokenize_chinese_lexical(item.answer)
            tokens = q_tokens + a_tokens
            self.doc_tokens.append(tokens)
            l = len(tokens)
            self.doc_len.append(l)
            total_len += l

            # 统计词频在多少文档中出现 (DF)
            seen = set(tokens)
            for w in seen:
                self.doc_freqs[w] = self.doc_freqs.get(w, 0) + 1

        self.avg_doc_len = (total_len / self.corpus_size) if total_len > 0 else 1.0

        # 计算 IDF: ln(1 + (N - n + 0.5) / (n + 0.5))
        for w, df in self.doc_freqs.items():
            self.idf[w] = math.log(1.0 + (self.corpus_size - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 5) -> List[Tuple[FAQItem, float]]:
        if not self.items:
            return []
        q_tokens = tokenize_chinese_lexical(query)
        if not q_tokens:
            return [(it, 0.0) for it in self.items[:top_k]]

        scores = [0.0] * self.corpus_size
        for i, tokens in enumerate(self.doc_tokens):
            if self.doc_len[i] == 0:
                continue
            # 统计该 doc 中的词频
            freqs: Dict[str, int] = {}
            for t in tokens:
                freqs[t] = freqs.get(t, 0) + 1

            doc_score = 0.0
            l = self.doc_len[i]
            for q in q_tokens:
                if q not in freqs:
                    continue
                tf = freqs[q]
                idf = self.idf.get(q, 0.1)
                # BM25 公式
                num = tf * (self.k1 + 1.0)
                den = tf + self.k1 * (1.0 - self.b + self.b * (l / self.avg_doc_len))
                doc_score += idf * (num / den)

            scores[i] = doc_score

        # 排序并返回
        ranked = sorted(
            [(self.items[i], scores[i]) for i in range(self.corpus_size)],
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked[:top_k]


class UniversalFAQRetriever:
    """
    通用 FAQ 混合检索器
    集成: BM25 词法检索 + 本地 CPU/远程 稠密向量检索 + RRF 融合
    """

    _embedding_cache: Dict[str, List[List[float]]] = {}
    _local_model_instance = None
    _local_tokenizer_instance = None
    _local_model_load_attempted = False

    def __init__(self, config: FAQInspectionConfig):
        self.config = config
        self.kb = config.knowledge_base
        self.bm25 = BM25Retriever(self.kb) if self.kb else None

    @classmethod
    def _get_kb_cache_key(cls, kb: List[FAQItem], emb_cfg: EmbeddingConfig) -> str:
        content = "|".join(f"{it.id}:{it.question}:{it.answer}" for it in kb)
        content += f"|{emb_cfg.provider_type}|{emb_cfg.local_model_name_or_path}|{emb_cfg.remote_model_name}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    @classmethod
    def _cosine_similarity(cls, vec_a: List[float], vec_b: List[float]) -> float:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot_product / (norm_a * norm_b)

    def _embed_texts(self, texts: List[str]) -> Optional[List[List[float]]]:
        """按配置获取一组文本的稠密向量表示"""
        emb_cfg = self.config.embedding_config
        if not emb_cfg or emb_cfg.provider_type == "disabled" or not texts:
            return None

        # 1. 远程 API 嵌入
        if emb_cfg.provider_type == "remote_api" and emb_cfg.api_url:
            try:
                import requests
                headers = {"Content-Type": "application/json"}
                if emb_cfg.api_key:
                    headers["Authorization"] = f"Bearer {emb_cfg.api_key}"
                payload = {
                    "input": texts,
                    "model": emb_cfg.remote_model_name or "text-embedding-3-small",
                }
                resp = requests.post(emb_cfg.api_url, json=payload, headers=headers, timeout=10)
                if resp.status_code == 200:
                    res_data = resp.json()
                    data_items = res_data.get("data", [])
                    data_items = sorted(data_items, key=lambda x: x.get("index", 0))
                    return [item.get("embedding", []) for item in data_items]
            except Exception as e:
                print(f"⚠️ [UniversalFAQRetriever] 远程 Embedding API 调用失败: {e}")

        # 2. 本地 CPU 简易向量模型 (优先本地已缓存或本地物理路径，杜绝公网长时间阻塞)
        if emb_cfg.provider_type == "local_cpu":
            try:
                import os
                import torch
                from transformers import AutoTokenizer, AutoModel

                if not UniversalFAQRetriever._local_model_load_attempted:
                    UniversalFAQRetriever._local_model_load_attempted = True
                    model_path = emb_cfg.local_model_name_or_path or "BAAI/bge-small-zh-v1.5"
                    try:
                        # 仅在本地路径存在或有本地缓存时加载，避免离线环境连接 HuggingFace 阻塞
                        is_local_dir = os.path.isdir(model_path)
                        UniversalFAQRetriever._local_tokenizer_instance = AutoTokenizer.from_pretrained(
                            model_path, local_files_only=not is_local_dir
                        ) if not is_local_dir else AutoTokenizer.from_pretrained(model_path)
                        UniversalFAQRetriever._local_model_instance = AutoModel.from_pretrained(
                            model_path, local_files_only=not is_local_dir
                        ) if not is_local_dir else AutoModel.from_pretrained(model_path)
                        UniversalFAQRetriever._local_model_instance.eval()
                    except Exception as load_err:
                        # 优雅静默回退，不阻断主流程
                        UniversalFAQRetriever._local_tokenizer_instance = None
                        UniversalFAQRetriever._local_model_instance = None

                tok = UniversalFAQRetriever._local_tokenizer_instance
                mdl = UniversalFAQRetriever._local_model_instance
                if tok and mdl:
                    encoded = tok(texts, padding=True, truncation=True, max_length=256, return_tensors="pt")
                    with torch.no_grad():
                        outputs = mdl(**encoded)
                        embeddings = outputs[0][:, 0]
                        embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
                        return embeddings.cpu().tolist()
            except Exception:
                pass

        # 3. 本地兜底特征哈希向量 (保证在无网络、无外部权重时依然具备极速向量相似度计算)
        return [self._hash_vector(t, dim=64) for t in texts]

    @staticmethod
    def _hash_vector(text: str, dim: int = 64) -> List[float]:
        """纯内置特征哈希向量兜底算法"""
        tokens = tokenize_chinese_lexical(text)
        vec = [0.0] * dim
        for t in tokens:
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
            idx = h % dim
            sign = 1.0 if (h // dim) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def _get_kb_vectors(self) -> Optional[List[List[float]]]:
        """获取知识库的预计算向量（内存缓存）"""
        if not self.kb:
            return None
        cache_key = self._get_kb_cache_key(self.kb, self.config.embedding_config)
        if cache_key in UniversalFAQRetriever._embedding_cache:
            return UniversalFAQRetriever._embedding_cache[cache_key]

        kb_texts = [f"{it.question} {it.answer}" for it in self.kb]
        vectors = self._embed_texts(kb_texts)
        if vectors:
            UniversalFAQRetriever._embedding_cache[cache_key] = vectors
        return vectors

    def search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        统一检索入口: 支持 lexical_only / vector_only / hybrid
        输出结构: [{'faq': FAQItem, 'score': float, 'retrieval_source': str}]
        """
        k = top_k or self.config.top_k or 3
        if not self.kb or not query.strip():
            return []

        mode = self.config.retrieval_mode or "hybrid"

        # 1. 词法检索结果
        lexical_results = self.bm25.search(query, top_k=len(self.kb)) if self.bm25 else []
        lexical_rank_map = {item.id: idx + 1 for idx, (item, _) in enumerate(lexical_results)}
        lexical_score_map = {item.id: score for item, score in lexical_results}

        # 2. 稠密向量检索结果
        dense_rank_map = {}
        dense_score_map = {}
        kb_vecs = self._get_kb_vectors()
        q_vec = None

        if mode in ("vector_only", "hybrid") and kb_vecs:
            q_vecs = self._embed_texts([query])
            if q_vecs:
                q_vec = q_vecs[0]
                dense_scores = [
                    (self.kb[i], self._cosine_similarity(q_vec, kb_vecs[i]))
                    for i in range(len(self.kb))
                ]
                dense_sorted = sorted(dense_scores, key=lambda x: x[1], reverse=True)
                dense_rank_map = {it.id: idx + 1 for idx, (it, _) in enumerate(dense_sorted)}
                dense_score_map = {it.id: sc for it, sc in dense_sorted}

        # 3. 结果融合
        final_items: List[Dict[str, Any]] = []

        if mode == "lexical_only" or not dense_rank_map:
            for item, sc in lexical_results[:k]:
                final_items.append({
                    "faq": item,
                    "score": round(float(sc), 4),
                    "retrieval_source": "lexical",
                })
        elif mode == "vector_only":
            sorted_dense = sorted(
                [(it, dense_score_map.get(it.id, 0.0)) for it in self.kb],
                key=lambda x: x[1],
                reverse=True,
            )
            for item, sc in sorted_dense[:k]:
                final_items.append({
                    "faq": item,
                    "score": round(float(sc), 4),
                    "retrieval_source": "dense_vector",
                })
        else:
            # 混合检索模式: 倒数排名融合算法 (RRF)
            # RRF_Score = w_lex / (60 + rank_lex) + w_dense / (60 + rank_dense)
            w_lex = self.config.hybrid_weights.get("lexical", 0.4)
            w_dense = self.config.hybrid_weights.get("dense", 0.6)
            rrf_constant = 60.0

            scored_candidates = []
            for item in self.kb:
                r_lex = lexical_rank_map.get(item.id, len(self.kb) + 1)
                r_dense = dense_rank_map.get(item.id, len(self.kb) + 1)

                rrf_score = (w_lex / (rrf_constant + r_lex)) + (w_dense / (rrf_constant + r_dense))
                scored_candidates.append((item, rrf_score, r_lex, r_dense))

            scored_candidates.sort(key=lambda x: x[1], reverse=True)
            for item, rrf_sc, r_lex, r_dense in scored_candidates[:k]:
                final_items.append({
                    "faq": item,
                    "score": round(float(rrf_sc), 6),
                    "retrieval_source": "hybrid_rrf",
                    "lexical_rank": r_lex,
                    "dense_rank": r_dense,
                })

        return final_items

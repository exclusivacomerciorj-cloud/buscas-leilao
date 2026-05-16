"""
LegalAnalyzer — IA Jurídica para análise de editais de leilão.

Pipeline:
  1. Download do PDF do edital
  2. Extração de texto (PyMuPDF ou OCR via Tesseract se escaneado)
  3. Análise estruturada com GPT-4o
  4. Retorna JSON com todos os riscos identificados

Campos analisados:
  - Ocupação (desocupado / ocupado / indefinido)
  - Dívidas (IPTU, condomínio, outras)
  - Ações judiciais impeditivas
  - Ônus reais (hipoteca, penhora, alienação fiduciária)
  - Restrições de venda
  - Prazo de desocupação
  - Observações críticas
"""

import asyncio
import io
import re
import tempfile
from pathlib import Path
from typing import Optional

import httpx
from openai import AsyncOpenAI

from app.core.config import get_settings
from app.core.logger import logger

settings = get_settings()

# ─── Prompt do sistema ────────────────────────────────────────────────────────

LEGAL_SYSTEM_PROMPT = """Você é um especialista jurídico em direito imobiliário brasileiro, 
com foco em leilões judiciais e extrajudiciais, retomadas bancárias e execuções hipotecárias.

Sua função é analisar editais de leilão e documentos imobiliários e extrair 
informações críticas de risco para investidores.

Você responde SEMPRE em JSON válido, sem texto fora do JSON.
Seja preciso, objetivo e conservador — na dúvida, sinalize como risco."""

LEGAL_USER_PROMPT = """Analise o seguinte texto de edital de leilão imobiliário e retorne um JSON 
com a estrutura abaixo. Preencha todos os campos com base no texto. 
Se uma informação não estiver explícita no texto, use null.

JSON de saída (preencha todos os campos):
{
  "ocupacao": {
    "status": "desocupado" | "ocupado" | "indefinido",
    "descricao": "texto explicando a situação de ocupação",
    "risco_despejo": true | false,
    "prazo_desocupacao_dias": null | número
  },
  "dividas": {
    "iptu": {
      "tem_debito": true | false,
      "valor_estimado": null | número,
      "responsabilidade": "arrematante" | "vendedor" | "indefinido"
    },
    "condominio": {
      "tem_debito": true | false,
      "valor_estimado": null | número,
      "responsabilidade": "arrematante" | "vendedor" | "indefinido"
    },
    "outras_dividas": [],
    "total_dividas_estimado": null | número
  },
  "acoes_judiciais": {
    "tem_acao": true | false,
    "tipos": [],
    "impeditiva": true | false,
    "descricao": null | "texto"
  },
  "onus_reais": {
    "hipoteca": true | false,
    "penhora": true | false,
    "alienacao_fiduciaria": true | false,
    "outros": [],
    "descricao": null | "texto"
  },
  "restricoes_venda": {
    "tem_restricao": true | false,
    "tipos": [],
    "descricao": null | "texto"
  },
  "imovel": {
    "descricao_completa": "texto",
    "matricula": null | "número",
    "registro_imoveis": null | "texto",
    "area_total": null | número,
    "area_util": null | número,
    "endereco_completo": null | "texto",
    "valor_avaliacao": null | número,
    "valor_minimo_1_leilao": null | número,
    "valor_minimo_2_leilao": null | número,
    "data_1_leilao": null | "data",
    "data_2_leilao": null | "data",
    "modalidade": null | "texto"
  },
  "score_risco": {
    "total": número de 0 a 100,
    "nivel": "baixo" | "medio" | "alto" | "critico",
    "fatores_risco": ["lista de fatores de risco identificados"],
    "pontos_positivos": ["lista de pontos positivos"],
    "recomendacao": "COMPRAR" | "ANALISAR_COM_CAUTELA" | "EVITAR",
    "justificativa": "texto explicando a recomendação"
  },
  "resumo_executivo": "parágrafo resumindo a oportunidade em linguagem simples para o investidor"
}

TEXTO DO EDITAL:
{edital_text}"""


# ─── Extração de texto do PDF ─────────────────────────────────────────────────

async def extract_text_from_pdf_url(pdf_url: str) -> Optional[str]:
    """Download e extração de texto de um PDF de edital."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(pdf_url)
            response.raise_for_status()
            pdf_bytes = response.content

        return extract_text_from_pdf_bytes(pdf_bytes)
    except Exception as e:
        logger.error(f"[Legal] Erro ao baixar PDF {pdf_url}: {e}")
        return None


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> Optional[str]:
    """Extrai texto de um PDF em bytes."""
    # Tenta PyMuPDF primeiro (mais rápido e preciso)
    text = _extract_with_pymupdf(pdf_bytes)
    if text and len(text.strip()) > 200:
        return text

    # Fallback: pdfplumber
    text = _extract_with_pdfplumber(pdf_bytes)
    if text and len(text.strip()) > 200:
        return text

    # Último recurso: OCR com Tesseract (para PDFs escaneados)
    logger.warning("[Legal] PDF parece escaneado, tentando OCR...")
    return _extract_with_ocr(pdf_bytes)


def _extract_with_pymupdf(pdf_bytes: bytes) -> Optional[str]:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_text = []
        for page in doc:
            pages_text.append(page.get_text())
        doc.close()
        return "\n\n".join(pages_text)
    except ImportError:
        logger.debug("[Legal] PyMuPDF não disponível")
        return None
    except Exception as e:
        logger.warning(f"[Legal] PyMuPDF falhou: {e}")
        return None


def _extract_with_pdfplumber(pdf_bytes: bytes) -> Optional[str]:
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n\n".join(text_parts)
    except ImportError:
        logger.debug("[Legal] pdfplumber não disponível")
        return None
    except Exception as e:
        logger.warning(f"[Legal] pdfplumber falhou: {e}")
        return None


def _extract_with_ocr(pdf_bytes: bytes) -> Optional[str]:
    """OCR usando pytesseract para PDFs escaneados."""
    try:
        import pytesseract
        from PIL import Image
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text_parts = []

        for page_num in range(min(len(doc), 10)):  # Limita a 10 páginas
            page = doc[page_num]
            # Renderiza a página como imagem (300 DPI)
            mat = fitz.Matrix(300 / 72, 300 / 72)
            clip = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [clip.width, clip.height], clip.samples)
            text = pytesseract.image_to_string(img, lang="por")
            text_parts.append(text)

        doc.close()
        return "\n\n".join(text_parts)
    except ImportError:
        logger.warning("[Legal] OCR não disponível (pytesseract/PIL/fitz)")
        return None
    except Exception as e:
        logger.error(f"[Legal] OCR falhou: {e}")
        return None


# ─── Análise com GPT-4o ───────────────────────────────────────────────────────

async def analyze_edital_with_ai(edital_text: str) -> Optional[dict]:
    """
    Envia o texto do edital para o GPT-4o e retorna a análise estruturada.
    """
    if not settings.OPENAI_API_KEY:
        logger.error("[Legal] OPENAI_API_KEY não configurada")
        return None

    # Limita o texto para não estourar o contexto (GPT-4o suporta 128k tokens)
    # ~3 chars por token → 100k tokens ≈ 300k chars
    max_chars = 280_000
    if len(edital_text) > max_chars:
        logger.warning(f"[Legal] Texto truncado: {len(edital_text)} → {max_chars} chars")
        edital_text = edital_text[:max_chars] + "\n\n[TEXTO TRUNCADO]"

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        logger.info(f"[Legal] Analisando edital ({len(edital_text)} chars) com GPT-4o...")

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": LEGAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": LEGAL_USER_PROMPT.format(edital_text=edital_text),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.1,  # Baixa temperatura para maior consistência
            max_tokens=4000,
        )

        import json
        result_text = response.choices[0].message.content
        result = json.loads(result_text)

        logger.info(
            f"[Legal] Análise concluída | "
            f"risco={result.get('score_risco', {}).get('nivel', '?')} | "
            f"recomendação={result.get('score_risco', {}).get('recomendacao', '?')}"
        )
        return result

    except Exception as e:
        logger.error(f"[Legal] Erro na análise com GPT-4o: {e}")
        return None


# ─── Classe principal ─────────────────────────────────────────────────────────

class LegalAnalyzer:
    """
    Analisa juridicamente um imóvel a partir do edital.

    Uso:
        analyzer = LegalAnalyzer()
        result = await analyzer.analyze_from_url("https://...")
        result = await analyzer.analyze_from_text("texto do edital...")
        result = await analyzer.analyze_from_bytes(pdf_bytes)
    """

    async def analyze_from_url(self, pdf_url: str) -> Optional[dict]:
        """Pipeline completo a partir da URL do edital."""
        logger.info(f"[Legal] Analisando edital: {pdf_url[:80]}...")

        text = await extract_text_from_pdf_url(pdf_url)
        if not text:
            logger.error("[Legal] Não foi possível extrair texto do PDF")
            return self._error_result("Falha na extração do PDF")

        return await self.analyze_from_text(text)

    async def analyze_from_bytes(self, pdf_bytes: bytes) -> Optional[dict]:
        """Pipeline completo a partir dos bytes do PDF."""
        text = extract_text_from_pdf_bytes(pdf_bytes)
        if not text:
            return self._error_result("Falha na extração do PDF")
        return await self.analyze_from_text(text)

    async def analyze_from_text(self, text: str) -> Optional[dict]:
        """Análise a partir do texto já extraído."""
        # Pré-processamento: limpa o texto
        text = self._clean_text(text)

        # Análise com GPT-4o
        result = await analyze_edital_with_ai(text)
        if not result:
            # Fallback: análise por regras sem IA
            logger.warning("[Legal] GPT-4o falhou, usando análise por regras")
            result = self._rule_based_analysis(text)

        return result

    def _clean_text(self, text: str) -> str:
        """Limpa e normaliza o texto extraído do PDF."""
        # Remove linhas muito curtas (artefatos de PDF)
        lines = text.split("\n")
        cleaned = [l for l in lines if len(l.strip()) > 2 or l.strip() == ""]

        # Normaliza espaços múltiplos
        text = "\n".join(cleaned)
        text = re.sub(r" {3,}", " ", text)
        text = re.sub(r"\n{4,}", "\n\n", text)

        return text.strip()

    def _rule_based_analysis(self, text: str) -> dict:
        """
        Análise baseada em regras como fallback quando a IA não está disponível.
        Busca por palavras-chave no texto do edital.
        """
        text_lower = text.lower()

        # Ocupação
        ocupado = any(w in text_lower for w in [
            "imóvel ocupado", "ocupado por", "encontra-se ocupado",
            "locatário", "inquilino", "possuidor",
        ])
        desocupado = any(w in text_lower for w in [
            "desocupado", "vago", "livre e desembaraçado",
            "sem ocupantes", "entregue desocupado",
        ])

        # Dívidas
        tem_iptu = any(w in text_lower for w in [
            "iptu", "imposto predial", "débito de iptu", "dívida de iptu"
        ])
        tem_condominio = any(w in text_lower for w in [
            "condomínio", "taxa condominial", "débito condominial"
        ])

        # Ações judiciais
        tem_acao = any(w in text_lower for w in [
            "ação judicial", "processo judicial", "penhora", "execução fiscal",
            "hipoteca", "arresto", "sequestro",
        ])

        # Calcula score de risco
        risk_score = 0
        risk_factors = []

        if ocupado:
            risk_score += 50
            risk_factors.append("Imóvel ocupado — risco de despejo")
        elif not desocupado:
            risk_score += 20
            risk_factors.append("Situação de ocupação indefinida")

        if tem_iptu:
            risk_score += 20
            risk_factors.append("Possível débito de IPTU")

        if tem_condominio:
            risk_score += 15
            risk_factors.append("Possível débito de condomínio")

        if tem_acao:
            risk_score += 30
            risk_factors.append("Ação judicial identificada")

        risk_score = min(100, risk_score)

        if risk_score >= 70:
            nivel, recomendacao = "critico", "EVITAR"
        elif risk_score >= 40:
            nivel, recomendacao = "medio", "ANALISAR_COM_CAUTELA"
        else:
            nivel, recomendacao = "baixo", "COMPRAR"

        return {
            "ocupacao": {
                "status": "ocupado" if ocupado else ("desocupado" if desocupado else "indefinido"),
                "descricao": "Análise por regras (IA indisponível)",
                "risco_despejo": ocupado,
                "prazo_desocupacao_dias": None,
            },
            "dividas": {
                "iptu": {"tem_debito": tem_iptu, "valor_estimado": None, "responsabilidade": "indefinido"},
                "condominio": {"tem_debito": tem_condominio, "valor_estimado": None, "responsabilidade": "indefinido"},
                "outras_dividas": [],
                "total_dividas_estimado": None,
            },
            "acoes_judiciais": {
                "tem_acao": tem_acao,
                "tipos": [],
                "impeditiva": False,
                "descricao": None,
            },
            "onus_reais": {
                "hipoteca": "hipoteca" in text_lower,
                "penhora": "penhora" in text_lower,
                "alienacao_fiduciaria": "alienação fiduciária" in text_lower,
                "outros": [],
                "descricao": None,
            },
            "restricoes_venda": {"tem_restricao": False, "tipos": [], "descricao": None},
            "imovel": {
                "descricao_completa": text[:500],
                "matricula": None, "registro_imoveis": None,
                "area_total": None, "area_util": None,
                "endereco_completo": None,
                "valor_avaliacao": None,
                "valor_minimo_1_leilao": None,
                "valor_minimo_2_leilao": None,
                "data_1_leilao": None, "data_2_leilao": None,
                "modalidade": None,
            },
            "score_risco": {
                "total": risk_score,
                "nivel": nivel,
                "fatores_risco": risk_factors,
                "pontos_positivos": ["Imóvel desocupado"] if desocupado else [],
                "recomendacao": recomendacao,
                "justificativa": "Análise automática por regras (sem IA)",
            },
            "resumo_executivo": f"Análise por regras: score de risco {risk_score}/100. {'Imóvel ocupado — requer atenção.' if ocupado else 'Situação de ocupação favorável.'}",
            "_source": "rule_based",
        }

    def _error_result(self, message: str) -> dict:
        return {
            "erro": message,
            "score_risco": {
                "total": 100,
                "nivel": "critico",
                "recomendacao": "EVITAR",
                "justificativa": f"Não foi possível analisar: {message}",
                "fatores_risco": ["Falha na análise"],
                "pontos_positivos": [],
            },
            "resumo_executivo": f"Análise falhou: {message}",
        }

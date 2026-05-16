import requests
import os
import time

url = os.environ["API_URL"] + "/api/v1/import/caixa"
print(f"Enviando para: {url}")

BAIRROS_ALVO = {
    "BARRA DA TIJUCA", "JACAREPAGUA", "RECREIO DOS BANDEIRANTES",
    "FREGUESIA (JACAREPAGUA)", "FREG DE JACAREPAGUA", "FREG JACAREPAGUA",
    "FREGUESIA JACAREPAGU", "FREG. JACAREPAGUA", "ITANHANGA", "CURICICA",
    "PECHINCHA", "TAQUARA", "GARDENIA AZUL", "ANIL",
    "BOTAFOGO", "COPACABANA", "GLORIA", "COSME VELHO",
    "SAO CONRADO", "TIJUCA", "ANDARAI", "GRAJAU", "MARACANA",
    "LARANJEIRAS", "CATETE", "FLAMENGO",
}

with open("caixa_rj.csv", "r", encoding="utf-8-sig", errors="replace") as f:
    lines = f.readlines()

lines = [l for l in lines if l.strip()]
print(f"Total linhas no CSV: {len(lines)}")

header = lines[0:2]
filtradas = []

for l in lines[2:]:
    partes = l.split(";")
    if len(partes) > 3:
        cidade = partes[2].strip()
        bairro = partes[3].strip()
        if cidade == "RIO DE JANEIRO" and bairro in BAIRROS_ALVO:
            filtradas.append(l)

print(f"Imoveis nos bairros alvo: {len(filtradas)}")

total = 0
parte = 1

for i in range(0, len(filtradas), 500):
    chunk = "".join(header + filtradas[i:i+500])
    try:
        r = requests.post(
            url,
            files={"file": ("caixa.csv", chunk.encode("utf-8"), "text/csv")},
            timeout=60
        )
        print(f"Parte {parte} status: {r.status_code}")
        print(f"Parte {parte} resposta: {r.text[:200]}")
        if r.status_code == 200:
            data = r.json()
            imported = data.get("imported", 0)
            total += imported
            print(f"Parte {parte}: {imported} imoveis novos")
        else:
            print(f"Parte {parte} erro HTTP {r.status_code}")
    except Exception as e:
        print(f"Parte {parte} excecao: {e}")
    parte += 1
    time.sleep(0.5)

print(f"TOTAL IMPORTADO: {total} imoveis")
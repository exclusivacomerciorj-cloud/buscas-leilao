import requests
import os
import time

url = os.environ["API_URL"] + "/api/v1/import/caixa"
print(f"Enviando para: {url}")

with open("caixa_rj.csv", "r", encoding="utf-8-sig", errors="replace") as f:
    lines = f.readlines()

lines = [l for l in lines if l.strip()]
print(f"Total linhas validas: {len(lines)}")

header = lines[0:2]
total = 0
parte = 1

for i in range(2, len(lines), 500):
    chunk = "".join(header + lines[i:i+500])
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

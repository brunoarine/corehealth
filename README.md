# corehealth

Monitora a saúde da rede MeshCore Brasil a partir do banco SQLite gerado pelo
CoreScope (caminho padrão: `db/meshcore.db`). Consulte `SCHEMA.md` para o esquema do banco e
`MESHCORE_PACKETS.md` para a estrutura dos pacotes.

## Relatório HTML

Gera uma página única, autocontida, em pt-BR e responsiva:

```sh
uv run src/corehealth/report.py
# ou, após `uv sync`:
corehealth-report
```

| Flag | Padrão | Descrição |
|---|---|---|
| `--db` | `db/meshcore.db` | Banco SQLite do CoreScope |
| `--output` | `output/index.html` | Arquivo de saída |
| `-t/--window` | `7d` | Janela de análise (ancorada no fim da captura) |
| `--min-adverts` | `3` | Critério: mediana diária de anúncios > valor |
| `--min-observers` | `5` | Critério: observadores distintos > valor |
| `--css-mode` | `inline` | `inline` (CSS embutido) ou `link` (externo) |
| `--strict` | — | Propaga erros de seção (útil em CI) |

## Scripts auxiliares

```sh
uv run src/corehealth/top_adverts.py --db db/meshcore.db -t 24h --repeaters-only --json --all
uv run src/corehealth/reach.py <NOME-OU-ID-DO-NÓ> -t 24h --json
uv run src/corehealth/neighbors.py <NOME-OU-ID-DO-NÓ> -t 7d --json
```

## Adicionar seções ao relatório

Escreva `build_*_section(db, window, ...) -> Section` em
`src/corehealth/report.py` e registre-a em `SECTION_BUILDERS`.
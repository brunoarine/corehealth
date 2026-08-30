# Plano — `src/corehealth/report.py`

Gerador de uma página HTML única e autocontida com a saúde da rede MeshCore Brasil.

## 1. Objetivo e escopo

- Novo script `src/corehealth/report.py`, executável via `uv run src/corehealth/report.py`.
- Gera `output/index.html`: **uma** página, autocontida, em **português do Brasil**.
- Estilo: **somente Pico CSS** (arquivos já presentes em `css/`). Sem Bootstrap/Tailwind/CSS custom
  além de umas poucas regras mínimas (ver §5).
- Estrutura preparada para múltiplas seções; nesta primeira versão só a seção
  **"Anúncios Excessivos"**.

## 2. Fonte de dados (reaproveitar código existente)

Os dois scripts já existentes expõem funções puras que podem ser importadas — evita `subprocess`,
JSON intermediário e re-abertura repetida do banco:

| Script | Função | Uso no report |
|---|---|---|
| `top_adverts.py` | `compute_stats(db, time_range, repeaters_only=True)` | lista de repetidores + nº de anúncios em 24h |
| `reach.py` | `compute_reach(db, node_key, time_range)` | lista de observadores que ouviram o nó em 24h |
| ambos | `parse_time_range`, `format_timedelta` | parsing do `-t/--window` |

Equivalente CLI (para conferência manual do resultado):

```
uv run src/corehealth/top_adverts.py --db db/meshcore.db -t 24h --repeaters-only --json --all
uv run src/corehealth/reach.py <NODE ID> -t 24h --json
```

Pontos de atenção descobertos na inspeção do código:

1. `compute_stats` **não** expõe a `public_key` completa no resultado — só `node_id`
   (`public_key[:4]`). `compute_reach` resolve o nó por prefixo de chave (>= 4 hex), o que funciona,
   mas pode levantar `SystemExit` em caso de prefixo ambíguo, e também pode casar por nome.
   → **Ação:** adicionar `public_key` à tupla/dicionário de resultado de `compute_stats`
   (alteração retrocompatível: incluir no dict do `print_json` e no retorno) e passar a chave
   completa para `compute_reach`. Se preferirmos não tocar em `top_adverts.py`, envolver a chamada
   em `try/except SystemExit` e registrar o nó como "indeterminado".
2. `compute_stats`/`compute_reach` fazem `sys.exit`/`SystemExit` quando não há dados.
   → No report, capturar e renderizar mensagem "sem dados no período" em vez de abortar.
3. Os dois scripts ancoram a janela no **fim da captura** (não no relógio de parede) e usam âncoras
   diferentes: `top_adverts` usa `max(transmissions.first_seen)`, `reach` usa
   `max(observations.timestamp)`. A diferença é pequena (segundos/minutos), mas deve ser
   documentada no rodapé da página ("janela ancorada no fim da captura").
4. Default de `--db` difere entre os scripts (`./meshcore.db` vs `./db/meshcore.db`).
   → No report o default será `db/meshcore.db`.

## 3. Regra da seção "Anúncios Excessivos"

Para a janela de 24h, considerando **apenas repetidores**:

```
incluir nó  ⇔  total_adverts > 2  E  nº_observadores_distintos_24h > 1
```

Implementação: 1 chamada a `compute_stats` + 1 chamada a `compute_reach` por nó que já passou no
filtro de anúncios (poda antes de consultar alcance). Custo medido: ~0,05 s por chamada; hoje são
18 nós candidatos de 83 → execução total bem abaixo de 1 s.

Resultado atual (validação do critério, `db/meshcore.db`, `-t 24h`): 10 nós
(91CD, A49C, 3776, 7024, 9E84, 6F13, BA1D, 6331, 2CE0, A0DF).

Parâmetros expostos como constantes/flags (`--min-adverts 2`, `--min-observers 1`) para facilitar
calibração futura.

Colunas da tabela (rótulos em pt-BR):

| Coluna | Origem |
|---|---|
| # | ordem (desc. por anúncios) |
| Nó | `node_name` |
| ID | `node_id` |
| Anúncios (24h) | `total_adverts` |
| Anúncios/dia | `adverts_per_day` |
| % direto (0 hop) | `pct_zero_hop` |
| % inundação | `pct_flood` |
| Observadores | `len(reach.observers)` |
| Observações | `reach.meta.total_observations` |
| Vizinhos | `len(neighbors)` + lista truncada em `<details>` ou `title=` |

Texto explicativo curto acima da tabela: o que a seção mede, por que importa (anúncios em excesso
consomem tempo de ar e inflam tabelas de rota) e como o critério é aplicado. Nota de rodapé da
seção: "Atualizado a cada 24 h".

## 4. Arquitetura do script

```python
# src/corehealth/report.py
CSS_FILE = "css/pico.min.css"          # ou pico.<tema>.min.css

@dataclass
class Section:
    id: str
    title: str
    body_html: str

def build_excessive_adverts_section(db, window, min_adverts, min_observers) -> Section
def render_page(sections, meta) -> str          # <head>, <main class="container">, <footer>
def render_table(headers, rows, aligns) -> str  # helper genérico, escapa tudo com html.escape
def main()                                      # argparse → coleta seções → escreve arquivo
```

Princípios:
- Cada seção é uma função `build_*_section() -> Section`; `SECTIONS = [build_excessive_adverts_section, ...]`
  para que novas seções sejam só um append (requisito "mais seções no futuro").
- Nenhuma dependência externa (stdlib + Pico). `pyproject.toml` permanece sem `dependencies`.
- HTML montado com f-strings + `html.escape`; sem engine de template.
- Se uma seção falhar (exceção), renderizar aviso na própria seção e continuar as outras
  (`--strict` para propagar o erro, útil em CI).

### CLI

```
uv run src/corehealth/report.py \
  [--db db/meshcore.db] [--output output/index.html] \
  [-t/--window 24h] [--min-adverts 2] [--min-observers 1] \
  [--theme jade|amber|...] [--css-mode inline|link] [--strict]
```

## 5. Como manter "Pico CSS apenas" e autocontido

- Ler `css/pico.min.css` (ou `css/pico.<tema>.min.css`, ~84 KB) e **embutir** em `<style>`
  → um único arquivo `output/index.html` sem dependência de rede ou de `css/` (default `--css-mode inline`).
- `--css-mode link` copia o CSS escolhido para `output/css/` e usa `<link rel="stylesheet">`
  (útil para desenvolvimento/inspeção).
- Usar semântica nativa do Pico: `<main class="container">`, `<article>` por seção, `<hgroup>`,
  `<table>` (envolta em `<div class="overflow-auto">`), `<small>`, `<mark>`, `<details>`.
- Regras próprias permitidas: apenas `<style>` mínimo para alinhar números à direita
  (`td.num{text-align:right}`) e `white-space:nowrap`. Nada de framework adicional.
- `<html lang="pt-BR">`, `<meta charset>`, `<meta name="viewport">`, `<title>CoreHealth — Saúde da rede MeshCore BR</title>`.
- Tema: `data-theme="dark"` ou automático (deixar Pico decidir por `prefers-color-scheme`).

## 6. Metadados / rodapé da página

- Janela analisada (ex. "últimas 24 h da captura"), `capture_start`/`capture_end` da captura.
- Data/hora de geração do relatório (UTC **e** America/Sao_Paulo via `zoneinfo`).
- Totais: anúncios no período, nós únicos, repetidores analisados, nós listados.
- Nota sobre origem dos dados (banco do CoreScope, `db/meshcore.db`).

## 7. Mudanças fora do script

1. `.gitignore`: adicionar `output/`.
2. `src/corehealth/top_adverts.py`: expor `public_key` nos resultados de `compute_stats`
   (e no `--json`, campo `public_key`) — ver §2.1. Sem quebra de saída de tabela/CSV.
3. Opcional: `pyproject.toml` → `[project.scripts] corehealth-report = "corehealth.report:main"`.
4. Opcional: `README.md` com instruções de geração.

## 8. Passos de implementação

1. `.gitignore` += `output/`.
2. Ajuste mínimo em `top_adverts.py` para expor `public_key`.
3. Esqueleto de `report.py`: argparse, `Section`, `render_page`, `render_table`, escrita do arquivo,
   inline do CSS. Gerar página com uma seção "em construção" e validar o layout Pico.
4. Implementar `build_excessive_adverts_section` com a regra do §3.
5. Conferir números contra as saídas JSON dos dois scripts CLI (mesmos 10 nós hoje).
6. Revisar textos em pt-BR (acentuação, terminologia MeshCore: "anúncio", "repetidor",
   "observador", "vizinhos", "salto/hop", "inundação/flood").
7. Testes manuais: banco inexistente, janela sem dados, seção vazia (mensagem "Nenhum nó
   ultrapassou os limites nas últimas 24 h — 🎉"), `--css-mode link`, abrir o HTML offline.

## 9. Critérios de aceitação

- `uv run src/corehealth/report.py` cria `output/index.html` sem erros, em < 5 s.
- Arquivo abre offline em navegador, estilizado só por Pico, responsivo no celular.
- Página 100 % em português do Brasil; HTML válido (`lang="pt-BR"`, charset, viewport).
- Seção "Anúncios Excessivos" lista exatamente os nós que satisfazem
  `anúncios > 2` **e** `observadores > 1` na janela de 24 h.
- `output/` ignorado pelo Git.
- Adicionar uma nova seção exige apenas escrever `build_*_section` e registrá-la na lista.

## 10. Dúvidas a confirmar

1. O critério (`anúncios > 2` **e** `observadores > 1`) parece marcar nós *bem* alcançados; a
   intenção é penalizar quem anuncia demais **e** é ouvido por muitos (desperdício amplificado),
   ou o segundo teste deveria ser um limiar mais alto (ex. observadores > 5)? Implemento como
   descrito, com flags para calibrar. Resposta: a intenção é penalizar quem anuncia emais e não está ilhado numa submalha.
2. Ordenação padrão da tabela: por anúncios (assumido) ou por observadores? Resposta: por anúncios totais
3. Tema/cor do Pico e claro/escuro/automático — preferência? Resposta: escuro
4. Fuso a exibir nos horários: UTC, America/Sao_Paulo, ou ambos (assumido: ambos)? Resposta: America/Sao_Paulo

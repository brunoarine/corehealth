#!/usr/bin/env python3
"""
report.py — Relatório de saúde da rede MeshCore Brasil.

Gera uma página HTML única e autocontida (CSS próprio de ``assets/corehealth.css``
embutido) em ``output/index.html``. A página é 100 % em português do Brasil,
funciona offline e é responsiva (desktop e celular). Nesta primeira versão,
contém a seção "Anúncios Excessivos".

Uso:
    uv run src/corehealth/report.py                    # db/meshcore.db → output/index.html
    uv run src/corehealth/report.py -t 48h
    uv run src/corehealth/report.py --css-mode link    # CSS externo (desenvolvimento)
    uv run src/corehealth/report.py --strict           # propaga erros (CI)

Para adicionar uma nova seção: escreva ``build_*_section() -> Section``
e registre-a em ``SECTION_BUILDERS``.
"""
from __future__ import annotations

import argparse
import html
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .neighbors import compute_neighbors
    from .reach import compute_reach
    from .top_adverts import compute_stats, parse_time_range
except ImportError:  # executado como script: uv run src/corehealth/report.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from neighbors import compute_neighbors
    from reach import compute_reach
    from top_adverts import compute_stats, parse_time_range

# --- Constantes -----------------------------------------------------------

TZ_BR = ZoneInfo("America/Sao_Paulo")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = "db/meshcore.db"
DEFAULT_OUTPUT = "output/index.html"
DEFAULT_WINDOW = timedelta(days=7)
DEFAULT_MIN_ADVERTS = 2
DEFAULT_MIN_OBSERVERS = 5
PAGE_TITLE = "CoreHealth — Saúde da rede MeshCore BR"
CSS_PATH = PROJECT_ROOT / "assets" / "corehealth.css"


# --- Infraestrutura de renderização ----------------------------------------

class Raw(str):
    """String que ``render_table`` NÃO deve escapar (HTML já montado)."""


@dataclass
class Section:
    """Uma seção da página (título + corpo HTML pré-renderizado)."""
    id: str
    title: str
    body_html: str
    stats: dict = field(default_factory=dict)


def fmt_br(x, nd=1):
    """Número com vírgula decimal (pt-BR)."""
    return f"{x:.{nd}f}".replace(".", ",")


def fmt_int(n):
    """Inteiro com separador de milhar (pt-BR)."""
    return f"{n:,}".replace(",", ".")


def window_short(window):
    """Rótulo curto da janela de análise: até 3 dias em horas, depois em dias.

    ``timedelta(hours=72)`` → ``'72h'``; ``timedelta(days=5)`` → ``'5d'``.
    """
    total_seconds = int(window.total_seconds())
    if total_seconds >= 4 * 86400:
        return f"{total_seconds // 86400}d"
    hours = total_seconds // 3600
    if hours:
        return f"{hours}h"
    return f"{total_seconds // 60}min"


# Faixas de anúncios/dia que definem a cor do nome do nó na tabela.
RATE_YELLOW = (3, 4)    # 3–4 anúncios/dia  → amarelo
RATE_ORANGE = (5, 8)    # 5–8 anúncios/dia  → laranja
RATE_RED_MIN = 8        # acima de 8        → vermelho


def rate_class(rate):
    """Classe CSS do nome do nó conforme anúncios/dia."""
    if rate > RATE_RED_MIN:
        return "rate-red"
    if rate >= RATE_ORANGE[0]:
        return "rate-orange"
    if rate >= RATE_YELLOW[0]:
        return "rate-yellow"
    return "rate-ok"


def fmt_dt_br(iso_or_ts):
    """Converte timestamp ISO-Z ou unixepoch para 'dd/mm/yyyy HH:MM' (Brasília)."""
    if isinstance(iso_or_ts, str):
        dt = datetime.fromisoformat(iso_or_ts.replace("Z", "+00:00"))
    else:
        dt = datetime.fromtimestamp(iso_or_ts, tz=timezone.utc)
    return dt.astimezone(TZ_BR).strftime("%d/%m/%Y %H:%M")


def render_table(headers, rows, num_cols=()):
    """Tabela HTML genérica. Todo conteúdo é escapado, exceto ``Raw``.

    Colunas em *num_cols* recebem a classe ``num`` (alinhadas à direita).
    Cada célula carrega ``data-label`` para o layout empilhado em telas
    estreitas (o rótulo aparece via CSS ``::before``).
    """
    def cell(value, tag, extra="", label=None):
        content = value if isinstance(value, Raw) else html.escape(str(value))
        data = f' data-label="{html.escape(label)}"' if label else ""
        return f"<{tag}{extra}{data}>{content}</{tag}>"

    thead = "".join(
        cell(h, "th", ' class="num"' if i in num_cols else "")
        for i, h in enumerate(headers)
    )
    body = []
    for row in rows:
        tds = "".join(
            cell(c, "td", ' class="num nowrap"' if i in num_cols else "",
                 label=headers[i])
            for i, c in enumerate(row)
        )
        body.append(f"<tr>{tds}</tr>")
    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{thead}</tr></thead><tbody>{''.join(body)}</tbody>"
        "</table></div>"
    )


def render_page(sections, chips, footer_lines, css_include, title=PAGE_TITLE):
    """Monta o documento HTML completo."""
    articles = "\n".join(
        f'<section class="card" id="{html.escape(s.id)}">\n'
        f'<header class="card-header"><h2>{html.escape(s.title)}</h2></header>\n'
        f'<div class="card-body">\n{s.body_html}\n</div>\n</section>'
        for s in sections
    )
    chips_html = "".join(
        f'<li class="chip">{html.escape(c)}</li>' for c in chips
    )
    footer_html = "".join(
        f"<li>{html.escape(l)}</li>" for l in footer_lines
    )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{css_include}
</head>
<body>
<header class="page-header">
  <div class="container header-inner">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true"></span>
      <div>
        <h1>CoreHealth</h1>
        <p class="tagline">Saúde da rede MeshCore Brasil</p>
      </div>
    </div>
    <ul class="meta-chips">{chips_html}</ul>
  </div>
</header>
<main class="container">
{articles}
</main>
<footer class="page-footer">
  <div class="container"><ul class="footer-list">{footer_html}</ul></div>
</footer>
</body>
</html>
"""


def build_css_include(css_mode, output_path):
    """Retorna a tag <style>/<link> e o nome do CSS copiado (ou ``None``)."""
    if not CSS_PATH.is_file():
        raise SystemExit(f"CSS não encontrado: {CSS_PATH}")
    name = CSS_PATH.name
    if css_mode == "inline":
        return f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", None
    css_dir = Path(output_path).resolve().parent / "css"
    css_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CSS_PATH, css_dir / name)
    return (f'<link rel="stylesheet" href="css/{name}">', name)


def render_stats_strip(items):
    """Faixa de indicadores: [(valor, rótulo), ...] → HTML."""
    cells = "".join(
        f'<div class="stat"><span class="stat-value">{html.escape(str(v))}'
        f'</span><span class="stat-label">{html.escape(l)}</span></div>'
        for v, l in items
    )
    return f'<div class="stats">{cells}</div>'


# --- Seções ----------------------------------------------------------------

def details_cell(names, empty_title):
    """Célula genérica: contagem + lista completa em <details>."""
    n = len(names)
    if not n:
        return Raw(f'<span title="{html.escape(empty_title)}">0</span>')
    listing = html.escape(", ".join(names))
    return Raw(f"<details><summary>{n}</summary><small>{listing}</small></details>")


def observers_cell(observer_names):
    """Célula 'Observadores'."""
    return details_cell(observer_names, "Nenhum observador no período")


def high_confidence_neighbors(db_path, pk, window):
    """Nomes dos vizinhos imediatos (primeiro salto RF) com confiança 'high'.

    Usa a mesma janela de análise informada na linha de comando.
    """
    _node, _capture, links = compute_neighbors(
        db_path, pk, time_range=window
    )
    return [l["name"] for l in links if l["confidence"] == "high"]


def neighbors_cell(names):
    """Célula 'Vizinhos': vizinhos de alta confiança ('—' se indeterminado)."""
    if names is None:
        return Raw('<span title="Vizinhos indeterminados">—</span>')
    return details_cell(names, "Nenhum vizinho de alta confiança no período")


CORESCOPE_BASE = "https://corescope.meshsorocaba.org"


def node_name_cell(name, pk, rate):
    """Nome do nó como link para o CoreScope, colorido conforme anúncios/dia.

    O CoreScope identifica o nó pela chave pública completa (``pk``).
    """
    cls = rate_class(rate)
    title = f"{fmt_br(rate)} anúncios/dia"
    url = f"{CORESCOPE_BASE}/#/nodes/{pk}"
    return Raw(
        f'<a class="node-rate {cls}" href="{html.escape(url)}"'
        f' title="{html.escape(title)}" target="_blank"'
        ' rel="noopener">' f"{html.escape(name)}</a>"
    )


def build_excessive_adverts_section(db_path, window, min_adverts,
                                    min_observers):
    """Seção 'Anúncios Excessivos'.

    Critério (apenas repetidores, janela de análise): incluir nó ⇔
    anúncios > min_adverts E observadores distintos > min_observers E
    nº de vizinhos de alta confiança ≥ 2.
    """
    try:
        capture_info, results = compute_stats(
            db_path, time_range=window, repeaters_only=True
        )
    except SystemExit as exc:
        body = ('<div class="notice notice-error"><p>Sem dados no período'
                + (f" ({exc})" if str(exc) else "")
                + ".</p></div>")
        return Section("anuncios-excessivos", "Anúncios Excessivos", body)

    rows = []
    for name, nid, role, total, rate, pz, pf, _nbrs, pk in results:
        if total <= min_adverts:
            continue  # poda antes de consultar alcance
        try:
            _node, reach_meta, observers = compute_reach(
                db_path, pk, time_range=window
            )
        except SystemExit:
            print(f"aviso: alcance indeterminado para {name} ({nid})",
                  file=sys.stderr)
            continue
        if len(observers) <= min_observers:
            continue
        try:
            vizinhos = high_confidence_neighbors(db_path, pk, window)
        except Exception as exc:  # noqa: BLE001 — vizinhos indeterminados
            print(f"aviso: vizinhos indeterminados para {name} ({nid}): {exc}",
                  file=sys.stderr)
            vizinhos = None
        if vizinhos is None or len(vizinhos) < 2:
            # Critério: nº de vizinhos ≥ 2 (indeterminado também não entra)
            continue
        rows.append((
            len(rows) + 1,
            node_name_cell(name, pk, rate),
            nid,
            fmt_br(rate),
            f"{fmt_br(pz)}%",
            f"{fmt_br(pf)}%",
            observers_cell([o["observer"] for o in observers]),
            neighbors_cell(vizinhos),
        ))

    window_label = f"em {window_short(window)}"
    intro = (
        "<p>Esta seção lista <strong>repetidores</strong> que enviaram "
        f"<mark>mais de {min_adverts} anúncios</mark> {window_label} "
        "da captura e que foram ouvidos por "
        f"<mark>mais de {min_observers} observador"
        f"{'es' if min_observers != 1 else ''} distinto"
        f"{'s' if min_observers != 1 else ''}</mark>, além de terem "
        "<mark>2 ou mais vizinhos</mark> — ou seja, nós que "
        "anunciam demais <em>e</em> não estão isolados numa região. "
        "Anúncios em excesso consomem tempo de ar e inflam as tabelas de "
        "rota de toda a rede.</p>"
        "<p>Para corrigir um repetidor configurado incorretamente, o operador "
        "deve acessar o CLI no gerenciamento remoto do repetidor e executar "
        "os comandos abaixo conforme os "
        "<a href=\"https://meshcore.com.br/valores-de-referencia/\" "
        "target=\"_blank\" rel=\"noopener\">valores de referência da "
        "comunidade</a>:</p>"
        "<p><code>set advert.interval 0</code><br>"
        "<code>set flood.advert.interval 23</code></p>"
    )
    stats_strip = render_stats_strip([
        (fmt_int(capture_info["total_adverts"]), "anúncios no período"),
        (fmt_int(capture_info["unique_nodes"]), "repetidores analisados"),
        (fmt_int(len(rows)), "nós listados"),
    ])

    if rows:
        table = render_table(
            headers=["#", "Nó", "ID", "Anúncios/dia",
                     "% direto (0 salto)", "% inundação", "Observadores",
                     "Vizinhos"],
            rows=rows,
            num_cols={0, 3, 4, 5},
        )
        body = intro + stats_strip + table
    else:
        body = intro + stats_strip + (
            f'<p class="empty">Nenhum nó ultrapassou os limites '
            f"{window_label} — 🎉</p>"
        )

    return Section(
        "anuncios-excessivos", "Anúncios Excessivos", body,
        stats={
            "total_adverts": capture_info["total_adverts"],
            "repeaters_analyzed": capture_info["unique_nodes"],
            "nodes_listed": len(rows),
            "capture_start": capture_info["start"],
            "capture_end": capture_info["end"],
        },
    )


# Ordem das seções na página — novas seções são só um append.
SECTION_BUILDERS = [build_excessive_adverts_section]


# --- Metadados / rodapé ------------------------------------------------------

def collect_meta(db_path, sections, window):
    """Coleta chips do cabeçalho e linhas do rodapé da página."""
    capture_start = capture_end = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT min(first_seen), max(first_seen) FROM transmissions "
            "WHERE payload_type = 4"
        ).fetchone()
        conn.close()
        capture_start, capture_end = row
    except sqlite3.Error:
        pass
    for s in sections:  # prefere os valores medidos pela seção, se houver
        capture_start = s.stats.get("capture_start", capture_start) or capture_start
        capture_end = s.stats.get("capture_end", capture_end) or capture_end

    window_label = window_short(window)
    generated_at = datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M")

    chips = [f"Janela: {window_label}"]
    if capture_start and capture_end:
        chips.append(
            f"Captura: {fmt_dt_br(capture_start)} – {fmt_dt_br(capture_end)}"
        )
    chips.append(f"Gerado em {generated_at}")

    lines = [f"Janela analisada: {window_label} da captura "
             "(janela ancorada no fim da captura)."]
    if capture_start and capture_end:
        lines.append(
            f"Captura: {fmt_dt_br(capture_start)} até {fmt_dt_br(capture_end)} "
            "(horário de Brasília)."
        )
    lines.append(f"Relatório gerado em {generated_at} (horário de Brasília).")
    lines.append("Site mantido por Bruno Arine (PY2TOZ)")
    return chips, lines


# --- CLI ---------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Gera o relatório HTML de saúde da rede MeshCore BR."
    )
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"Caminho do banco SQLite (padrão: {DEFAULT_DB})")
    p.add_argument("--output", default=DEFAULT_OUTPUT,
                   help=f"Arquivo de saída (padrão: {DEFAULT_OUTPUT})")
    p.add_argument("-t", "--window", type=parse_time_range,
                   default=DEFAULT_WINDOW, metavar="DURAÇÃO",
                   help="Janela de análise, ex.: 24h, 7d (padrão: 7d)")
    p.add_argument("--min-adverts", type=int, default=DEFAULT_MIN_ADVERTS,
                   help="Mínimo de anúncios para considerar excessivo "
                        f"(critério: anúncios > valor; padrão: {DEFAULT_MIN_ADVERTS})")
    p.add_argument("--min-observers", type=int, default=DEFAULT_MIN_OBSERVERS,
                   help="Mínimo de observadores distintos (critério: "
                        f"observadores > valor; padrão: {DEFAULT_MIN_OBSERVERS})")
    p.add_argument("--css-mode", choices=["inline", "link"], default="inline",
                   help="inline: CSS embutido (padrão); link: CSS externo "
                        "copiado para output/css/")
    p.add_argument("--strict", action="store_true",
                   help="Propaga erros de seção em vez de renderizar aviso")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not Path(args.db).is_file():
        raise SystemExit(f"Banco de dados não encontrado: {args.db}")

    sections = []
    for build in SECTION_BUILDERS:
        try:
            sections.append(
                build(args.db, args.window, args.min_adverts,
                      args.min_observers)
            )
        except Exception as exc:  # noqa: BLE001 — seção não derruba a página
            if args.strict:
                raise
            print(f"aviso: seção {build.__name__} falhou: {exc}",
                  file=sys.stderr)
            sections.append(Section(
                build.__name__, "Seção indisponível",
                f'<div class="notice notice-error"><p><mark>Não foi possível '
                f"gerar esta seção:</mark> {html.escape(str(exc))}</p></div>",
            ))

    css_include, _css_name = build_css_include(args.css_mode, args.output)
    chips, footer_lines = collect_meta(args.db, sections, args.window)
    page = render_page(sections, chips, footer_lines, css_include)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"Relatório gerado: {out} ({out.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()

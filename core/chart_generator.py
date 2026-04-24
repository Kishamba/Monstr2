import io
import sqlite3
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'trades.db'
)


def generate_report_chart(days: int = 7) -> tuple[bytes, str]:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    # ── Загружаем сделки ─────────────────────────────────────────────
    since = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()

    trades = []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT timestamp, pnl_value, exit_reason
                FROM trades
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
            """, (since,)).fetchall()
            trades = [
                {
                    'ts':     r[0],
                    'pnl':    r[1] or 0,
                    'reason': r[2] or '?',
                }
                for r in rows
            ]
    except Exception:
        trades = []

    total     = len(trades)
    wins      = sum(1 for t in trades if t['pnl'] > 0)
    losses    = total - wins
    total_pnl = sum(t['pnl'] for t in trades)
    win_rate  = wins / total * 100 if total > 0 else 0

    # ── Цвета ────────────────────────────────────────────────────────
    BG     = '#0d1117'
    GREEN  = '#00d26a'
    RED    = '#ff4757'
    GRAY   = '#8b949e'
    WHITE  = '#e6edf3'

    # ── Фигура: 1 строка, 2 колонки ──────────────────────────────────
    fig, (ax_pie, ax_line) = plt.subplots(
        1, 2,
        figsize=(12, 5),
        facecolor=BG,
        gridspec_kw={'width_ratios': [1, 2]}
    )
    fig.subplots_adjust(left=0.06, right=0.97,
                        top=0.82, bottom=0.15, wspace=0.35)

    # ── Заголовок ─────────────────────────────────────────────────────
    sign = '+' if total_pnl >= 0 else ''
    fig.suptitle(
        f'Monster 2.0 — последние {days} дней  •  '
        f'{total} сделок  •  '
        f'WR {win_rate:.0f}%  •  '
        f'P&L {sign}{total_pnl:.2f}$',
        color=WHITE,
        fontsize=13,
        fontweight='bold',
        y=0.97,
    )

    # ══════════════════════════════════════════════════════════════════
    # ЛЕВЫЙ БЛОК — круговая диаграмма Win/Loss
    # ══════════════════════════════════════════════════════════════════
    ax_pie.set_facecolor(BG)

    if total == 0:
        ax_pie.text(
            0.5, 0.5, 'Нет данных',
            ha='center', va='center',
            color=GRAY, fontsize=12,
            transform=ax_pie.transAxes,
        )
        ax_pie.axis('off')
    else:
        sizes   = [wins, losses] if losses > 0 else [wins]
        colors  = [GREEN, RED]   if losses > 0 else [GREEN]
        labels  = (
            [f'Win\n{wins}', f'Loss\n{losses}']
            if losses > 0 else [f'Win\n{wins}']
        )
        explode = [0.04] + [0] * (len(sizes) - 1)

        wedges, texts, autotexts = ax_pie.pie(
            sizes,
            labels=labels,
            colors=colors,
            autopct='%1.0f%%',
            startangle=90,
            explode=explode,
            textprops={'color': WHITE, 'fontsize': 11},
            wedgeprops={'linewidth': 2, 'edgecolor': BG},
            pctdistance=0.72,
        )
        for at in autotexts:
            at.set_fontsize(13)
            at.set_fontweight('bold')
            at.set_color(WHITE)

    ax_pie.set_title('Win / Loss', color=WHITE, fontsize=11, pad=10)

    # ══════════════════════════════════════════════════════════════════
    # ПРАВЫЙ БЛОК — накопленный P&L по времени
    # ══════════════════════════════════════════════════════════════════
    ax_line.set_facecolor(BG)
    ax_line.spines[:].set_color('#30363d')
    ax_line.tick_params(colors=GRAY, labelsize=9)
    ax_line.set_xlabel('Дата', color=GRAY, fontsize=9)
    ax_line.set_ylabel('Накопленный P&L ($)', color=GRAY, fontsize=9)
    ax_line.set_title(
        'Накопленный доход / убыток',
        color=WHITE, fontsize=11, pad=10,
    )
    ax_line.grid(
        True, color='#21262d',
        linewidth=0.7, linestyle='--', alpha=0.7,
    )

    if not trades:
        ax_line.text(
            0.5, 0.5, 'Нет данных',
            ha='center', va='center',
            color=GRAY, fontsize=12,
            transform=ax_line.transAxes,
        )
    else:
        timestamps = []
        cumulative = []
        running = 0.0
        for t in trades:
            try:
                dt = datetime.fromisoformat(
                    t['ts'].replace('Z', '+00:00')
                )
            except Exception:
                continue
            running += t['pnl']
            timestamps.append(dt)
            cumulative.append(running)

        if timestamps:
            line_color = GREEN if cumulative[-1] >= 0 else RED

            ax_line.plot(
                timestamps, cumulative,
                color=line_color,
                linewidth=2.0,
                zorder=3,
            )
            ax_line.fill_between(
                timestamps, cumulative, 0,
                alpha=0.15,
                color=line_color,
                zorder=2,
            )
            ax_line.axhline(
                0,
                color=GRAY,
                linewidth=0.8,
                linestyle='-',
                alpha=0.5,
                zorder=1,
            )
            ax_line.scatter(
                [timestamps[-1]], [cumulative[-1]],
                color=line_color,
                s=60, zorder=5,
            )
            sign_f = '+' if cumulative[-1] >= 0 else ''
            ax_line.annotate(
                f'{sign_f}{cumulative[-1]:.2f}$',
                xy=(timestamps[-1], cumulative[-1]),
                xytext=(8, 4),
                textcoords='offset points',
                color=line_color,
                fontsize=10,
                fontweight='bold',
            )
            ax_line.xaxis.set_major_formatter(
                mdates.DateFormatter('%d.%m %H:%M')
            )
            plt.setp(
                ax_line.xaxis.get_majorticklabels(),
                rotation=30, ha='right',
                color=GRAY, fontsize=8,
            )
            ax_line.yaxis.set_tick_params(labelcolor=GRAY)

    # ── Сохраняем ─────────────────────────────────────────────────────
    buf = io.BytesIO()
    plt.savefig(
        buf, format='png', dpi=140,
        bbox_inches='tight',
        facecolor=BG,
    )
    plt.close(fig)
    buf.seek(0)

    # ── Текстовый отчёт ───────────────────────────────────────────────
    sign = '+' if total_pnl >= 0 else ''
    report = (
        f"📊 <b>Отчёт за {days} дней</b>\n\n"
        f"Сделок:   <b>{total}</b>  "
        f"(✅{wins} / ❌{losses})\n"
        f"Win Rate: <b>{win_rate:.0f}%</b>\n"
        f"P&L:      <b>{sign}{total_pnl:.2f}$</b>\n"
    )

    if trades:
        from collections import defaultdict
        by_day = defaultdict(float)
        for t in trades:
            day = t['ts'][:10]
            by_day[day] += t['pnl']
        if by_day:
            best_day  = max(by_day.items(), key=lambda x: x[1])
            worst_day = min(by_day.items(), key=lambda x: x[1])
            report += (
                f"\n🏆 Лучший день:  "
                f"<b>{best_day[0]}  {best_day[1]:+.2f}$</b>\n"
                f"💀 Худший день:  "
                f"<b>{worst_day[0]}  {worst_day[1]:+.2f}$</b>"
            )

    return buf.read(), report

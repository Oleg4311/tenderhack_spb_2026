import styles from './StatsBar.module.css'

const SOURCE_META = {
  wildberries: { color: '#cb11ab', label: 'WB' },
  ozon: { color: '#005bff', label: 'Ozon' },
  yandex_market: { color: '#ff9b00', label: 'ЯМ' },
  runet: { color: '#00b896', label: 'Рунет' },
}

const STATUS_COLORS = {
  ok: '#16a34a',
  empty: '#d97706',
  blocked: '#dc2626',
  error: '#dc2626',
}

export default function StatsBar({ results, activeSource, onSourceChange }) {
  const groups = Object.entries(results.groups || {}).map(([source, group]) => ({ source, ...group }))
  const total = results.summary?.totalFound || 0

  return (
    <div className={styles.wrap}>
      <div className={styles.totalBadge}>
        Найдено: <strong>{total}</strong> предложений
      </div>

      <div className={styles.tabs}>
        <button
          className={`${styles.tab} ${activeSource === 'all' ? styles.tabActive : ''}`}
          onClick={() => onSourceChange('all')}
        >
          Все источники
        </button>
        {groups.map(r => {
          const meta = SOURCE_META[r.source] || { color: '#888', label: r.source }
          const prices = r.items.map(i => i.price).filter(Boolean)
          const min = prices.length ? Math.min(...prices) : 0
          return (
            <button
              key={r.source}
              className={`${styles.tab} ${activeSource === r.source ? styles.tabActive : ''}`}
              onClick={() => onSourceChange(r.source)}
            >
              <span className={styles.tabDot} style={{ background: meta.color }} />
              <span className={styles.tabName}>{meta.label}</span>
              <span className={styles.tabCount}>{r.count}</span>
              {r.status !== 'ok' && (
                <span className={styles.statusBadge} style={{ color: STATUS_COLORS[r.status] || '#888' }}>
                  {r.status}
                </span>
              )}
              {min > 0 && <span className={styles.tabPrice}>от {fmtPrice(min)}</span>}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function fmtPrice(v) {
  return new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', maximumFractionDigits: 0 }).format(v)
}

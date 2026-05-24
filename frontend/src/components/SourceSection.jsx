import ProductCard from './ProductCard.jsx'
import styles from './SourceSection.module.css'

const SOURCE_META = {
  yandex_market: { label: 'Яндекс Маркет', color: '#ff9b00', bg: 'rgba(255,155,0,0.08)' },
  ozon:          { label: 'Ozon',           color: '#005bff', bg: 'rgba(0,91,255,0.08)'   },
  wildberries:   { label: 'Wildberries',    color: '#cb11ab', bg: 'rgba(203,17,171,0.08)' },
  runet:         { label: 'Рунет',          color: '#00b896', bg: 'rgba(0,184,150,0.08)'  },
}

const EMPTY_ICONS = {
  blocked: '🔒',
  error:   '⚡',
  empty:   '🔍',
  skipped: '⏭',
}

const EMPTY_MESSAGES = {
  blocked: 'Источник временно недоступен',
  error:   'Ошибка при получении данных',
  empty:   'По этому запросу ничего не найдено',
  skipped: 'Не используется для этой категории',
}

function fmtPrice(v) {
  return new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', maximumFractionDigits: 0 }).format(v)
}

function EmptyState({ status, errorReason, meta }) {
  const icon = EMPTY_ICONS[status] || '○'
  const msg  = EMPTY_MESSAGES[status] || 'Нет данных'
  return (
    <div className={styles.emptyState} style={{ borderColor: meta.color + '22' }}>
      <div className={styles.emptyIcon}>{icon}</div>
      <div className={styles.emptyMsg}>{msg}</div>
      {errorReason && (
        <div className={styles.emptyReason}>{errorReason}</div>
      )}
    </div>
  )
}

export default function SourceSection({ data }) {
  const meta   = SOURCE_META[data.source] || { label: data.source, color: '#888', bg: 'rgba(128,128,128,0.08)' }
  const items  = data.items || []
  const prices = items.map(i => i.price).filter(Boolean)
  const min    = prices.length ? Math.min(...prices) : 0
  const max    = prices.length ? Math.max(...prices) : 0

  if (data.status === 'skipped') return null

  const isEmpty = items.length === 0

  return (
    <section className={styles.section}>
      <div className={styles.header}>
        <div className={styles.titleRow}>
          <div className={styles.sourceBadge} style={{ background: meta.bg, borderColor: meta.color + '44' }}>
            <span className={styles.sourceDot} style={{ background: meta.color }} />
            <span className={styles.sourceName} style={{ color: meta.color }}>{meta.label}</span>
          </div>
          {!isEmpty && <div className={styles.countBadge}>{data.count} предложений</div>}
          {data.status && data.status !== 'ok' && !isEmpty && (
            <div className={styles.statusBadge}>{data.status}</div>
          )}
        </div>

        {!isEmpty && data.errorReason && (
          <div className={styles.warning}>{data.errorReason}</div>
        )}

        {min > 0 && (
          <div className={styles.priceRange}>
            {min !== max
              ? <span>от <strong>{fmtPrice(min)}</strong> до <strong>{fmtPrice(max)}</strong></span>
              : <span>от <strong>{fmtPrice(min)}</strong></span>
            }
          </div>
        )}
      </div>

      {isEmpty ? (
        <EmptyState status={data.status} errorReason={data.errorReason} meta={meta} />
      ) : (
        <div className={styles.gridScroll}>
          <div className={styles.grid}>
            {items.map((item, idx) => (
              <ProductCard key={`${item.url}-${idx}`} item={item} accentColor={meta.color} />
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

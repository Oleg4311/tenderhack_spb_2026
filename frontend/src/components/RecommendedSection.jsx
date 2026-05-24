import ProductCard from './ProductCard.jsx'
import styles from './RecommendedSection.module.css'

const SOURCE_COLORS = {
  yandex_market: '#ff9b00',
  ozon:          '#005bff',
  wildberries:   '#cb11ab',
  runet:         '#00b896',
}

export default function RecommendedSection({ items }) {
  if (!items || items.length === 0) return null

  return (
    <section className={styles.section}>
      <div className={styles.header}>
        <div className={styles.titleRow}>
          <span className={styles.star}>★</span>
          <span className={styles.title}>Рекомендуемые</span>
          <span className={styles.count}>{items.length} товаров</span>
        </div>
        <span className={styles.subtitle}>Лучшие предложения по релевантности из всех источников</span>
      </div>
      <div className={styles.gridScroll}>
        <div className={styles.grid}>
          {items.map((item, idx) => (
            <ProductCard
              key={`rec-${item.url}-${idx}`}
              item={item}
              accentColor={SOURCE_COLORS[item.source] || '#888'}
            />
          ))}
        </div>
      </div>
    </section>
  )
}

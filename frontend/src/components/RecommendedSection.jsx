import { useState, useEffect, useRef, useCallback } from 'react'
import ProductCard from './ProductCard.jsx'
import styles from './RecommendedSection.module.css'

const SOURCE_COLORS = {
  yandex_market: '#ff9b00',
  ozon:          '#005bff',
  wildberries:   '#cb11ab',
  runet:         '#00b896',
}

const PAGE_SIZE = 6
const LOAD_MORE = 6

export default function RecommendedSection({ items }) {
  const [visible, setVisible] = useState(Math.min(PAGE_SIZE, items.length))
  const sentinelRef = useRef(null)

  useEffect(() => {
    setVisible(Math.min(PAGE_SIZE, items.length))
  }, [items])

  const loadMore = useCallback(() => {
    setVisible(v => Math.min(v + LOAD_MORE, items.length))
  }, [items.length])

  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) loadMore() },
      { rootMargin: '120px' }
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [loadMore])

  if (!items || items.length === 0) return null

  const shown = items.slice(0, visible)
  const hasMore = visible < items.length
  const remaining = items.length - visible

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
      <div className={styles.grid}>
        {shown.map((item, idx) => (
          <ProductCard
            key={`rec-${item.url}-${idx}`}
            item={item}
            accentColor={SOURCE_COLORS[item.source] || '#888'}
          />
        ))}
      </div>
      {hasMore && (
        <div ref={sentinelRef} className={styles.sentinel}>
          <button className={styles.loadMoreBtn} onClick={loadMore}>
            Показать ещё {Math.min(LOAD_MORE, remaining)} из {remaining}
          </button>
        </div>
      )}
    </section>
  )
}

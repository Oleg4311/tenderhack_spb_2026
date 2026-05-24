import SourceSection from './SourceSection.jsx'
import RecommendedSection from './RecommendedSection.jsx'
import SkeletonGrid from './SkeletonGrid.jsx'
import styles from './ResultsSection.module.css'

function groupList(results) {
  return Object.entries(results.groups || {}).map(([source, group]) => ({
    source,
    ...group,
  }))
}

export default function ResultsSection({ results, loading, activeSource }) {
  if (loading) return <SkeletonGrid />
  if (!results) return null

  const groups = groupList(results)
  const filtered = activeSource === 'all'
    ? groups
    : groups.filter(r => r.source === activeSource)

  const hasAny = filtered.some(r => r.items.length > 0)

  if (!hasAny) {
    return (
      <div className={styles.noResults}>
        <div className={styles.noResultsIcon}>?</div>
        <div className={styles.noResultsTitle}>Ничего не найдено</div>
        <div className={styles.noResultsSub}>Попробуйте изменить запрос, категорию или регион</div>
      </div>
    )
  }

  const recommended = results.recommended || []
  const showRecommended = activeSource === 'all' && recommended.length > 0

  return (
    <div className={styles.wrap}>
      {showRecommended && <RecommendedSection items={recommended} />}
      {filtered.map(sourceResult => (
        <SourceSection key={sourceResult.source} data={sourceResult} />
      ))}
    </div>
  )
}


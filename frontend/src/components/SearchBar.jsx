import { useEffect, useState, useRef } from 'react'
import styles from './SearchBar.module.css'
import { fetchSuggest } from '../utils/api.js'

const REGIONS = [
  'Москва', 'Санкт-Петербург', 'Новосибирск',
  'Екатеринбург', 'Казань', 'Нижний Новгород', 'Краснодар',
  'Ростов-на-Дону', 'Уфа', 'Самара', 'Омск',
]

export default function SearchBar({ onSearch, loading }) {
  const [query, setQuery] = useState('')
  const [region, setRegion] = useState('Москва')
  const [focused, setFocused] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  const [correctedQuery, setCorrectedQuery] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    if (query.trim().length < 2) {
      setSuggestions([])
      setCorrectedQuery('')
      return undefined
    }
    const timer = setTimeout(async () => {
      try {
        const data = await fetchSuggest(query.trim())
        setSuggestions(data.suggestions || [])
        setCorrectedQuery(data.correctedQuery || '')
      } catch {
        setSuggestions([])
        setCorrectedQuery('')
      }
    }, 300)
    return () => clearTimeout(timer)
  }, [query])

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!query.trim() || loading) return
    onSearch({ query: query.trim(), region })
  }

  const submitSuggestion = (value) => {
    setQuery(value)
    setSuggestions([])
    onSearch({ query: value, region })
  }

  const dropdownItems = [
    ...(correctedQuery && correctedQuery !== query.trim() ? [correctedQuery] : []),
    ...suggestions,
  ].filter((value, index, arr) => value && arr.indexOf(value) === index)

  return (
    <form className={styles.form} onSubmit={handleSubmit}>
      <div className={`${styles.wrap} ${focused ? styles.wrapFocused : ''}`}>
        {/* Иконка поиска */}
        <svg className={styles.searchIcon} viewBox="0 0 24 24" fill="none">
          <circle cx="11" cy="11" r="7.5" stroke="currentColor" strokeWidth="1.8"/>
          <path d="M17 17L21 21" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
        </svg>

        <input
          ref={inputRef}
          className={styles.input}
          type="text"
          placeholder="Введите товар: ноутбук, шины R18, куртка зимняя..."
          value={query}
          onChange={e => setQuery(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          disabled={loading}
          autoComplete="off"
        />

        {focused && dropdownItems.length > 0 && (
          <ul className={styles.suggestions}>
            {dropdownItems.map(item => (
              <li key={item}>
                <button type="button" onMouseDown={() => submitSuggestion(item)}>
                  {item === correctedQuery && correctedQuery !== query.trim() && (
                    <span className={styles.suggestionKind}>Исправить</span>
                  )}
                  {item}
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className={styles.divider} />

        {/* Регион */}
        <div className={styles.regionWrap}>
          <svg className={styles.regionIcon} viewBox="0 0 24 24" fill="none">
            <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z" stroke="currentColor" strokeWidth="1.8"/>
            <circle cx="12" cy="9" r="2.5" stroke="currentColor" strokeWidth="1.8"/>
          </svg>
          <select
            className={styles.regionSelect}
            value={region}
            onChange={e => setRegion(e.target.value)}
            disabled={loading}
          >
            {REGIONS.map(r => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </div>

        <button
          className={`${styles.btn} ${loading ? styles.btnLoading : ''}`}
          type="submit"
          disabled={loading || !query.trim()}
        >
          {loading ? (
            <span className={styles.loader}>
              <span /><span /><span />
            </span>
          ) : (
            <>
              <span>Найти</span>
              <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
                <path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </>
          )}
        </button>
      </div>
      <div className={styles.examples}>
        {['шины 205/55 R16', 'ноутбук lenovo', 'куртка зимняя мужская'].map(example => (
          <button
            key={example}
            type="button"
            disabled={loading}
            onClick={() => submitSuggestion(example)}
          >
            {example}
          </button>
        ))}
      </div>
    </form>
  )
}

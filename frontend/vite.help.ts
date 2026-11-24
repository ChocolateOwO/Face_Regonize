import { cpSync, existsSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Plugin } from 'vite'

const help = fileURLToPath(new URL('../help/', import.meta.url))
const languages = { en: 'English', th: 'ไทย', ja: '日本語', zh: '中文' }
type Language = keyof typeof languages

const pages = Object.keys(languages) as Language[]
const pageFile = (language: Language) => language === 'en' ? 'index.html' : `guide-${language}.html`

function guide(language: Language) {
  const template = readFileSync(resolve(help, 'standalone.html'), 'utf8')
  const fragment = readFileSync(resolve(help, `${language}.html`), 'utf8')
    .replace(/href="\/app\/faces\/"/g, 'href="/"')
    .replace(/data-shot="([a-z0-9-]+)"/g, (_, shot: string) => {
      const preferred = `img/${language}-light-${shot}.webp`
      const fallback = `img/en-light-${shot}.webp`
      const image = existsSync(resolve(help, preferred)) ? preferred : fallback
      return `src="${image}" loading="lazy" decoding="async"`
    })
  const links = pages.map((code) =>
    `<a href="${pageFile(code)}" lang="${code}"${code === language ? ' aria-current="page"' : ''}>${languages[code]}</a>`
  ).join(' ')
  return template
    .replace('{{language}}', language)
    .replace('{{languages}}', links)
    .replace('{{guide}}', () => fragment)
}

export function bundledHelp(): Plugin {
  return {
    name: 'bundled-help',
    configureServer(server) {
      server.middlewares.use('/help', (request, response, next) => {
        if (request.method !== 'GET' && request.method !== 'HEAD') return next()
        const path = request.url?.split('?', 1)[0]
        const page = pages.find((code) => path === `/${pageFile(code)}`)
        if (page) {
          response.setHeader('Content-Type', 'text/html; charset=utf-8')
          response.end(request.method === 'HEAD' ? undefined : guide(page))
          return
        }
        if (path && /^\/img\/[a-z0-9-]+\.webp$/.test(path)) {
          const image = resolve(help, path.slice(1))
          if (existsSync(image)) {
            response.setHeader('Content-Type', 'image/webp')
            response.end(request.method === 'HEAD' ? undefined : readFileSync(image))
            return
          }
        }
        response.statusCode = 404
        response.end()
      })
    },
    writeBundle({ dir }) {
      if (!dir) throw new Error('Help requires a Vite output directory')
      const directory = resolve(dir, 'help')
      cpSync(resolve(help, 'img'), resolve(directory, 'img'), { recursive: true })
      for (const language of pages) {
        writeFileSync(resolve(directory, pageFile(language)), guide(language))
      }
    },
  }
}

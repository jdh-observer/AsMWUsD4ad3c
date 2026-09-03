// render.js — rend des grappes de citations en texte auteur-date via citation-js
// (même moteur que le JDH : @citation-js/core + plugin-csl, template APA).
// Entrée (stdin JSON): { bib: [CSL-JSON...], clusters: [ [id1, id2], ... ] }
// Sortie (stdout JSON): { labels: ["(Rosenzweig, 2003)", "(Smith, 2019a; Smith, 2019b)", ...] }
const { Cite } = require('@citation-js/core')
require('@citation-js/plugin-csl')

let raw = ''
process.stdin.on('data', (d) => (raw += d))
process.stdin.on('end', () => {
  const { bib, clusters } = JSON.parse(raw)
  const cite = new Cite(bib) // charge TOUTE la biblio -> désambiguïsation year-suffix (fix #379)
  const labels = clusters.map((ids) => {
    try {
      return cite
        .format('citation', {
          template: 'apa',
          lang: 'en-US',
          format: 'text',
          entry: ids,
        })
        .trim()
    } catch (e) {
      return `[?: ${ids.join(', ')}]`
    }
  })
  process.stdout.write(JSON.stringify({ labels }))
})

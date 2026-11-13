// Shared style for the handbook's figures: one look for every figure.
// Draw at true print size: the canvas unit is 1 mm, and a figure is at most 85 mm wide (one column).
// Use explicit coordinates; give equal things equal sizes; centre things on shared axes.
#import "@preview/cetz:0.4.2"

#let garnet = rgb("{{MAIN}}")     // the main colour: the one thing to emphasise (the name is historical)
#let charcoal = rgb("{{ACCENT}}")  // the accent: lines, arrows, secondary fills
#let tint = rgb("{{TINT}}")       // box fills
#let ink = rgb("#1a1a1a")       // text
#let grey = rgb("#777777")      // small notes
#let line-w = {{LINE}}
#let corner = {{CORNER}}             // box corner radius in mm; use it for boxes you draw yourself
#let pill = {{PILL}}              // true: boxes are rounded to half their height
#let radius-of(w, h) = if pill { calc.min(w, h) / 2 } else { corner }
#let box-fill = "{{BOX_FILL}}"     // "tintline", "outline" or "solid"
#let soft-fill = if box-fill == "outline" { white } else { tint }
#let soft-stroke = if box-fill == "solid" { none } else if box-fill == "outline" { line-w + charcoal } else { line-w + garnet }

// Put `#show: figure-page` at the top of every fig.typ.
#let figure-page(body) = {
  set page(width: auto, height: auto, margin: 2pt, fill: white)
  set text(font: ("{{FONT}}", "Georgia"), size: 7pt, fill: ink)
  set par(leading: 0.42em, justify: false)
  body
}

// A box centred at `at` (x, y in mm), `w` by `h` mm, text centred inside.
// kind: "soft" (the default box: tint with a main-colour outline unless the theme says otherwise),
//       "key" (main colour, white text: the emphasis), "dark" (accent, white text),
//       "plain" (white, accent outline), "ghost" (dashed accent outline).
// align-left: true for lists. name: an anchor name for arrows ("a.east", "a.north", ...).
#let node(at, w, h, body, kind: "soft", name: none, align-left: false, size: none) = {
  import cetz.draw: *
  let fill = if kind == "key" { garnet } else if kind == "dark" { charcoal } else if kind == "soft" { soft-fill } else { white }
  let stroke = if kind == "soft" { soft-stroke } else if kind == "plain" { line-w + charcoal }
    else if kind == "ghost" { (paint: charcoal, thickness: line-w, dash: "dashed") } else { none }
  let color = if kind in ("key", "dark") { white } else { ink }
  let (x, y) = at
  rect((x - w / 2, y - h / 2), (x + w / 2, y + h / 2), radius: radius-of(w, h), fill: fill, stroke: stroke, name: name)
  let t = if size == none { text(fill: color, body) } else { text(fill: color, size: size, body) }
  content(at, box(width: (w - 1.6) * 1mm, align(if align-left { left } else { center }, t)))
}

// An arrow from a to b (coordinates or anchors). dashed: for feedback. both: arrowheads at both ends.
#let arrow(a, b, dashed: false, both: false, color: charcoal) = {
  import cetz.draw: *
  let st = (paint: color, thickness: line-w, dash: if dashed { "dashed" } else { none })
  let m = (fill: color, scale: 0.55)
  line(a, b, stroke: st, mark: if both { (start: ">", end: ">", ..m) } else { (end: ">", ..m) })
}

// A path through several points with an arrow at the end (for elbows: feedback loops, merges).
#let path(..pts, dashed: false, head: true) = {
  import cetz.draw: *
  let st = (paint: charcoal, thickness: line-w, dash: if dashed { "dashed" } else { none })
  line(..pts, stroke: st, mark: if head { (end: ">", fill: charcoal, scale: 0.55) } else { none })
}

// Free text at a point. anchor: "center", "west", "east", "north", "south", ...
#let label(pos, body, anchor: "center", color: ink, size: none, style: "normal", weight: "regular") = {
  // no `import cetz.draw: *` here: it would shadow the `anchor` parameter with cetz's anchor()
  let t = if size == none { text(fill: color, style: style, weight: weight, body) }
    else { text(fill: color, size: size, style: style, weight: weight, body) }
  cetz.draw.content(pos, t, anchor: anchor)
}

// A small tag above or beside a box (for "Bottleneck", "Start", "Fin").
#let tag(pos, body, kind: "key") = {
  import cetz.draw: *
  let fill = if kind == "key" { garnet } else { charcoal }
  content(pos, box(fill: fill, inset: (x: 1.6pt, y: 1.2pt), radius: 1.5pt, text(fill: white, size: 6.5pt, weight: "bold", body)))
}

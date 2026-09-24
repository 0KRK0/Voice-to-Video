"""Rendering for every visual primitive.

One function per `VisualPrimitive`, each drawing a single frame at a given
progress in ``[0, 1]``. Animation is therefore a pure function of time, which
means any frame can be rendered on its own — that is what makes storyboard
thumbnails free, and what makes rendering parallelisable later without changing
a line of this file.

Layout is expressed as fractions of the frame, never in pixels, so every
primitive works at preview resolution, at 1080p, and in portrait.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from vtv.animation.canvas import Canvas, format_number, nice_ticks
from vtv.animation.theme import (
    Theme,
    ease_out_back,
    ease_out_cubic,
    golden_angle_positions,
    mix,
    stagger,
    with_alpha,
)
from vtv.contracts.visual_language import (
    ChartKind,
    ChartSpec,
    ComparisonSpec,
    Emphasis,
    MapSpec,
    NetworkSpec,
    TimelineSpec,
    TypographySpec,
)


def _emphasis_scale(emphasis: Emphasis) -> float:
    return {Emphasis.SUBTLE: 0.85, Emphasis.NORMAL: 1.0, Emphasis.STRONG: 1.12}[emphasis]


def _accent_rule(canvas: Canvas, y: int, progress: float, width_fraction: float = 0.14) -> None:
    """The small accent bar that appears above a headline. A recurring motif is
    what makes a set of shots feel designed rather than assembled."""
    theme = canvas.theme
    full = theme.safe_width * width_fraction
    grown = full * ease_out_cubic(min(1.0, progress * 2.2))
    if grown < 2:
        return
    height = max(3, theme.scale(0.007))
    canvas.rounded_rect(
        (theme.margin, y, theme.margin + grown, y + height),
        height / 2,
        fill=theme.accent,
    )


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

def draw_typography(canvas: Canvas, spec: TypographySpec, progress: float) -> None:
    theme = canvas.theme
    font = theme.font("display" if spec.emphasis is Emphasis.STRONG else "headline")
    max_width = int(theme.safe_width * 0.94)

    block = canvas.wrap(spec.headline, font, max_width)
    # Long headlines step down the type scale rather than overflowing. Three
    # lines is the practical limit of what a viewer reads while listening.
    if len(block.lines) > 3:
        font = theme.font("title")
        block = canvas.wrap(spec.headline, font, max_width)

    subline_block = None
    if spec.subline:
        subline_font = theme.font("body")
        subline_block = canvas.wrap(spec.subline, subline_font, max_width)

    total_height = block.height + (subline_block.height + theme.scale(0.045) if subline_block else 0)
    top = (theme.height - total_height) // 2
    _accent_rule(canvas, top - theme.scale(0.055), progress)

    highlight = {word.lower(): theme.accent for word in spec.highlight if word}
    total_words = sum(len(line.split()) for line in block.lines)

    if spec.reveal == "word_by_word":
        revealed = math.ceil(total_words * ease_out_cubic(min(1.0, progress * 1.8)))
        visible = max(1, revealed)
    else:
        visible = None

    origin_x = theme.margin
    if spec.reveal == "slide":
        origin_x += int((1 - ease_out_cubic(min(1.0, progress * 2))) * theme.scale(0.06))

    canvas.text_block(
        block,
        font,
        (origin_x, top),
        theme.foreground,
        highlight=highlight or None,
        visible_words=visible,
    )

    if subline_block:
        appears = ease_out_cubic(max(0.0, (progress - 0.35) / 0.4))
        canvas.text_block(
            subline_block,
            theme.font("body"),
            (theme.margin, top + block.height + theme.scale(0.045)),
            with_alpha(theme.muted, appears),
        )


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def draw_chart(canvas: Canvas, spec: ChartSpec, progress: float) -> None:
    theme = canvas.theme
    eased = ease_out_cubic(min(1.0, progress * 1.5)) if spec.animate_in else 1.0

    top = theme.margin
    if spec.title:
        title_font = theme.font("title")
        block = canvas.wrap(spec.title, title_font, theme.safe_width)
        _accent_rule(canvas, top, progress)
        canvas.text_block(block, title_font, (theme.margin, top + theme.scale(0.03)), theme.foreground)
        top += block.height + theme.scale(0.06)

    plot = (
        theme.margin + theme.scale(0.10),
        top + (theme.scale(0.055) if spec.y_label else theme.scale(0.02)),
        theme.width - theme.margin,
        theme.height - theme.margin - theme.scale(0.075),
    )

    if spec.kind is ChartKind.PIE:
        _draw_pie(canvas, spec, eased, plot)
        return

    values = [point.value for series in spec.series for point in series.points]
    if not values:
        return
    lowest = min(0.0, min(values))
    highest = max(values)
    if math.isclose(highest, lowest):
        highest = lowest + 1.0

    ticks = nice_ticks(lowest, highest)
    axis_top, axis_bottom = plot[1], plot[3]
    axis_left, axis_right = plot[0], plot[2]
    span = (ticks[-1] - ticks[0]) or 1.0

    def y_for(value: float) -> float:
        return axis_bottom - ((value - ticks[0]) / span) * (axis_bottom - axis_top)

    # Gridlines and value labels.
    label_font = theme.font("micro")
    for tick in ticks:
        y = y_for(tick)
        canvas.draw.line(
            [(axis_left, y), (axis_right, y)], fill=with_alpha(theme.muted, 0.22), width=1
        )
        canvas.text(
            (axis_left - theme.scale(0.012), y),
            format_number(tick),
            label_font,
            with_alpha(theme.muted, 0.9),
            anchor="rm",
        )

    labels = [point.label for point in spec.series[0].points]
    count = max(1, len(labels))
    band = (axis_right - axis_left) / count

    if spec.kind in {ChartKind.COLUMN, ChartKind.BAR}:
        series_count = len(spec.series)
        # Cap the group width so a two-value chart reads as a comparison rather
        # than two slabs filling the frame.
        group_width = min(band * 0.62, theme.width * 0.22 * series_count)
        bar_width = group_width / series_count
        group_offset = (band - group_width) / 2
        for series_index, series in enumerate(spec.series):
            # P1-1. A locked entity colour wins over the categorical wheel,
            # so the same subject is the same colour in every scene and in
            # every re-render. A series that is not an entity falls through.
            colour = theme.colour_for(series.name, theme.series_colour(series_index))
            for point_index, point in enumerate(series.points):
                local = stagger(point_index, len(series.points), eased, overlap=0.72)
                grown = ease_out_cubic(local)
                x0 = (
                    axis_left
                    + band * point_index
                    + group_offset
                    + bar_width * series_index
                )
                baseline = y_for(max(ticks[0], 0.0))
                target = y_for(point.value)
                y0 = baseline - (baseline - target) * grown
                canvas.rounded_rect(
                    (x0, min(y0, baseline), x0 + bar_width * 0.88, max(y0, baseline)),
                    min(bar_width * 0.18, theme.scale(0.01)),
                    fill=colour,
                )
                if grown > 0.75:
                    canvas.text(
                        (x0 + bar_width * 0.44, y0 - theme.scale(0.016)),
                        format_number(point.value),
                        theme.font("micro"),
                        with_alpha(theme.foreground, (grown - 0.75) / 0.25),
                        anchor="mb",
                    )
    else:
        for series_index, series in enumerate(spec.series):
            # P1-1. A locked entity colour wins over the categorical wheel,
            # so the same subject is the same colour in every scene and in
            # every re-render. A series that is not an entity falls through.
            colour = theme.colour_for(series.name, theme.series_colour(series_index))
            points = [
                (axis_left + band * (index + 0.5), y_for(point.value))
                for index, point in enumerate(series.points)
            ]
            visible = max(2, math.ceil(len(points) * eased))
            drawn = points[:visible]
            if spec.kind is ChartKind.AREA and len(drawn) >= 2:
                baseline = y_for(max(ticks[0], 0.0))
                canvas.draw.polygon(
                    [*drawn, (drawn[-1][0], baseline), (drawn[0][0], baseline)],
                    fill=with_alpha(colour, 0.22),
                )
            if spec.kind is ChartKind.SCATTER:
                for x, y in drawn:
                    canvas.circle((x, y), theme.scale(0.009), fill=colour)
            elif len(drawn) >= 2:
                canvas.line(drawn, colour, width=max(3, theme.scale(0.006)))
                for x, y in drawn:
                    canvas.circle((x, y), theme.scale(0.008), fill=theme.background, outline=colour, width=max(2, theme.scale(0.004)))

    # Category labels.
    for index, label in enumerate(labels):
        canvas.text(
            (axis_left + band * (index + 0.5), axis_bottom + theme.scale(0.016)),
            label[:22],
            label_font,
            with_alpha(theme.muted, 0.95),
            anchor="ma",
        )

    if spec.y_label:
        # Above the plot, never beside it: an axis label that collides with the
        # top tick is the single most common chart defect.
        canvas.text(
            (axis_left - theme.scale(0.012), axis_top - theme.scale(0.034)),
            spec.y_label,
            theme.font("micro"),
            with_alpha(theme.muted, 0.9),
        )

    if len(spec.series) > 1:
        _draw_legend(canvas, [s.name for s in spec.series], axis_left, axis_top - theme.scale(0.03))


def _draw_legend(canvas: Canvas, names: list[str], x: float, y: float) -> None:
    theme = canvas.theme
    font = theme.font("micro")
    cursor = x
    for index, name in enumerate(names):
        # The legend must agree with the plot, so it resolves identically.
        colour = theme.colour_for(name, theme.series_colour(index))
        size = theme.scale(0.012)
        canvas.rounded_rect((cursor, y, cursor + size, y + size), size / 3, fill=colour)
        canvas.text((cursor + size * 1.5, y + size / 2), name[:24], font, theme.muted, anchor="lm")
        cursor += size * 1.5 + canvas.measure(name[:24], font)[0] + theme.scale(0.03)


def _draw_pie(canvas: Canvas, spec: ChartSpec, eased: float, plot: tuple[float, float, float, float]) -> None:
    theme = canvas.theme
    points = spec.series[0].points
    total = sum(max(0.0, point.value) for point in points) or 1.0
    cx = (plot[0] + plot[2]) / 2
    cy = (plot[1] + plot[3]) / 2
    radius = min(plot[2] - plot[0], plot[3] - plot[1]) * 0.38

    start = -90.0
    for index, point in enumerate(points):
        sweep = (max(0.0, point.value) / total) * 360.0 * eased
        canvas.draw.pieslice(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start,
            start + sweep,
            fill=theme.colour_for(point.label, theme.series_colour(index)),
        )
        if sweep > 12:
            mid = math.radians(start + sweep / 2)
            canvas.text(
                (cx + math.cos(mid) * radius * 1.22, cy + math.sin(mid) * radius * 1.22),
                f"{point.label[:18]} {format_number(point.value)}",
                theme.font("micro"),
                theme.foreground,
                anchor="mm",
            )
        start += sweep
    # Donut hole: easier to read than a solid pie, and leaves room for a total.
    canvas.circle((cx, cy), radius * 0.52, fill=theme.background)


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def draw_timeline(canvas: Canvas, spec: TimelineSpec, progress: float) -> None:
    theme = canvas.theme
    eased = ease_out_cubic(min(1.0, progress * 1.35))

    top = theme.margin
    if spec.title:
        title_font = theme.font("title")
        block = canvas.wrap(spec.title, title_font, theme.safe_width)
        _accent_rule(canvas, top, progress)
        canvas.text_block(block, title_font, (theme.margin, top + theme.scale(0.03)), theme.foreground)
        top += block.height + theme.scale(0.05)

    events = spec.events
    count = len(events)
    vertical = spec.orientation == "vertical" or (theme.is_portrait and count > 3)

    if vertical:
        axis_x = theme.margin + theme.scale(0.05)
        axis_top = top + theme.scale(0.03)
        axis_bottom = theme.height - theme.margin
        canvas.draw.line(
            [(axis_x, axis_top), (axis_x, axis_top + (axis_bottom - axis_top) * eased)],
            fill=with_alpha(theme.muted, 0.5),
            width=max(2, theme.scale(0.004)),
        )
        for index, event in enumerate(events):
            local = stagger(index, count, eased, overlap=0.55)
            if local <= 0:
                continue
            y = axis_top + (axis_bottom - axis_top) * ((index + 0.5) / count)
            radius = theme.scale(0.011) * ease_out_back(local)
            canvas.circle((axis_x, y), radius, fill=theme.accent)
            canvas.text(
                (axis_x + theme.scale(0.035), y - theme.scale(0.014)),
                event.when,
                theme.font("label"),
                with_alpha(theme.accent, local),
                anchor="lm",
            )
            block = canvas.wrap(
                event.label, theme.font("body"), theme.safe_width - theme.scale(0.09)
            )
            canvas.text_block(
                block,
                theme.font("body"),
                (int(axis_x + theme.scale(0.035)), int(y + theme.scale(0.004))),
                with_alpha(theme.foreground, local),
            )
        return

    axis_y = theme.height * 0.56
    axis_left = theme.margin + theme.scale(0.02)
    axis_right = theme.width - theme.margin - theme.scale(0.02)
    canvas.draw.line(
        [(axis_left, axis_y), (axis_left + (axis_right - axis_left) * eased, axis_y)],
        fill=with_alpha(theme.muted, 0.5),
        width=max(2, theme.scale(0.004)),
    )

    for index, event in enumerate(events):
        local = stagger(index, count, eased, overlap=0.5)
        if local <= 0:
            continue
        x = axis_left + (axis_right - axis_left) * ((index + 0.5) / count)
        radius = theme.scale(0.013) * ease_out_back(local)
        canvas.circle((x, axis_y), radius, fill=theme.accent)
        canvas.text(
            (x, axis_y - theme.scale(0.045)),
            event.when,
            theme.font("title"),
            with_alpha(theme.accent, local),
            anchor="mm",
        )
        block = canvas.wrap(event.label, theme.font("label"), int((axis_right - axis_left) / count * 0.92))
        canvas.text_block(
            block,
            theme.font("label"),
            (int(x - block.width / 2), int(axis_y + theme.scale(0.035))),
            with_alpha(theme.foreground, local),
            align="center",
        )
        if event.detail:
            canvas.text(
                (x, axis_y + theme.scale(0.035) + block.height + theme.scale(0.012)),
                event.detail[:60],
                theme.font("micro"),
                with_alpha(theme.muted, local),
                anchor="ma",
            )


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _network_positions(spec: NetworkSpec, theme: Theme) -> dict[str, tuple[float, float]]:
    cx, cy = theme.width / 2, theme.height * 0.54
    radius = min(theme.safe_width, theme.safe_height) * 0.36
    keys = [node.key for node in spec.nodes]

    if spec.layout == "layered":
        # A process reads left to right, or top to bottom in portrait.
        positions: dict[str, tuple[float, float]] = {}
        count = len(keys)
        for index, key in enumerate(keys):
            fraction = (index + 0.5) / count
            if theme.is_portrait:
                positions[key] = (cx, theme.margin + theme.safe_height * fraction)
            else:
                positions[key] = (theme.margin + theme.safe_width * fraction, cy)
        return positions

    if spec.layout == "radial" and keys:
        positions = {keys[0]: (cx, cy)}
        for key, (dx, dy) in zip(
            keys[1:], golden_angle_positions(len(keys) - 1, radius), strict=True
        ):
            positions[key] = (cx + dx, cy + dy)
        return positions

    return {
        key: (cx + dx, cy + dy)
        for key, dx, dy in (
            (key, dx, dy)
            for key, (dx, dy) in zip(
                keys, golden_angle_positions(len(keys), radius), strict=True
            )
        )
    }


def draw_network(canvas: Canvas, spec: NetworkSpec, progress: float) -> None:
    theme = canvas.theme
    eased = ease_out_cubic(min(1.0, progress * 1.3))
    positions = _network_positions(spec, theme)

    top = theme.margin
    if spec.title:
        title_font = theme.font("title")
        block = canvas.wrap(spec.title, title_font, theme.safe_width)
        _accent_rule(canvas, top, progress)
        canvas.text_block(block, title_font, (theme.margin, top + theme.scale(0.03)), theme.foreground)

    # Edges first so nodes sit on top of them.
    for index, edge in enumerate(spec.edges):
        local = (
            stagger(index, max(1, len(spec.edges)), eased, overlap=0.4)
            if spec.animation == "propagate"
            else eased
        )
        if local <= 0.01:
            continue
        start = positions.get(edge.source)
        end = positions.get(edge.target)
        if not start or not end:
            continue
        tip = (
            start[0] + (end[0] - start[0]) * local,
            start[1] + (end[1] - start[1]) * local,
        )
        colour = with_alpha(theme.muted, 0.65)
        if edge.directed:
            canvas.arrow(start, tip, colour, width=max(2, theme.scale(0.0035)), head=theme.scale(0.016))
        else:
            canvas.line([start, tip], colour, width=max(2, theme.scale(0.0035)))
        if edge.label and local > 0.85:
            canvas.text(
                ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2 - theme.scale(0.016)),
                edge.label[:28],
                theme.font("micro"),
                with_alpha(theme.muted, (local - 0.85) / 0.15),
                anchor="mm",
            )

    node_font = theme.font("label")
    for index, node in enumerate(spec.nodes):
        local = (
            stagger(index, len(spec.nodes), eased, overlap=0.65)
            if spec.animation in {"build", "appear"}
            else eased
        )
        if local <= 0.01:
            continue
        x, y = positions[node.key]
        label = node.label[:26]
        text_width = canvas.measure(label, node_font)[0]
        pad = theme.scale(0.018)
        half_w = (text_width / 2 + pad) * ease_out_back(local)
        half_h = (theme.scale(0.026)) * ease_out_back(local)
        # A locked entity keeps its colour as the node's outline and fill,
        # which is the strongest continuity signal a diagram has.
        node_colour = theme.colour_for(node.label, theme.accent)
        fill = (
            node_colour
            if index == 0 and spec.layout == "radial"
            else theme.background
        )
        canvas.rounded_rect(
            (x - half_w, y - half_h, x + half_w, y + half_h),
            half_h,
            fill=fill,
            outline=with_alpha(node_colour, local),
            width=max(2, theme.scale(0.003)),
        )
        if local > 0.5:
            canvas.text(
                (x, y),
                label,
                node_font,
                with_alpha(theme.legible_on(fill), (local - 0.5) / 0.5),
                anchor="mm",
            )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def draw_comparison(canvas: Canvas, spec: ComparisonSpec, progress: float) -> None:
    theme = canvas.theme
    eased = ease_out_cubic(min(1.0, progress * 1.4))
    scale = _emphasis_scale(spec.emphasis)

    pad = theme.scale(0.035)
    gap = theme.scale(0.045)
    mid = theme.width / 2

    def panel_height(side: object, inner_width: int) -> float:
        """Measure a panel rather than filling the frame with it.

        Stretching both panels to full height leaves a large empty area under
        two short lists, which reads as a layout mistake even though nothing is
        technically wrong."""
        title_block = canvas.wrap(side.title, theme.font("title"), inner_width)  # type: ignore[attr-defined]
        height = pad * 2 + title_block.height + theme.scale(0.028)
        for point in side.points:  # type: ignore[attr-defined]
            block = canvas.wrap(point, theme.font("body"), inner_width - theme.scale(0.03))
            height += block.height + theme.scale(0.018)
        return height

    boxes: list[tuple[float, float, float, float]]
    if theme.is_portrait:
        inner = int(theme.safe_width - pad * 2)
        heights = [panel_height(spec.left, inner), panel_height(spec.right, inner)]
        total = sum(heights) + gap
        top_y: float = max(float(theme.margin), (theme.height - total) / 2)
        boxes = [
            (theme.margin, top_y, theme.width - theme.margin, top_y + heights[0]),
            (
                theme.margin,
                top_y + heights[0] + gap,
                theme.width - theme.margin,
                top_y + heights[0] + gap + heights[1],
            ),
        ]
    else:
        inner = int(mid - gap - theme.margin - pad * 2)
        height = max(
            panel_height(spec.left, inner),
            panel_height(spec.right, inner),
            theme.safe_height * 0.34,
        )
        height = min(height, theme.safe_height)
        top_y = (theme.height - height) / 2
        boxes = [
            (theme.margin, top_y, mid - gap, top_y + height),
            (mid + gap, top_y, theme.width - theme.margin, top_y + height),
        ]

    for index, (side, box) in enumerate(zip([spec.left, spec.right], boxes, strict=True)):
        local = ease_out_cubic(stagger(index, 2, eased, overlap=0.55))
        if local <= 0.01:
            continue
        x0, y0, x1, y1 = box
        slide = (1 - local) * theme.scale(0.05) * (1 if index == 0 else -1)
        x0, x1 = x0 - slide, x1 - slide
        colour = theme.colour_for(
            side.title, theme.accent if index == 0 else theme.secondary
        )

        canvas.rounded_rect(
            (x0, y0, x1, y1),
            theme.scale(0.022),
            fill=with_alpha(mix(theme.background, colour, 0.10), local),
            outline=with_alpha(colour, local * 0.7),
            width=max(2, theme.scale(0.003)),
        )

        title_font = theme.font("title")
        title_block = canvas.wrap(side.title, title_font, int(x1 - x0 - pad * 2))
        canvas.text_block(
            title_block,
            title_font,
            (int(x0 + pad), int(y0 + pad)),
            with_alpha(colour, local),
        )

        cursor = y0 + pad + title_block.height + theme.scale(0.028)
        body_font = theme.font("body")
        for point_index, point in enumerate(side.points):
            point_local = stagger(point_index, max(1, len(side.points)), local, overlap=0.6)
            if point_local <= 0.02:
                continue
            block = canvas.wrap(point, body_font, int(x1 - x0 - pad * 2 - theme.scale(0.03)))
            canvas.circle(
                (x0 + pad + theme.scale(0.008), cursor + block.line_height * 0.45),
                theme.scale(0.006) * point_local,
                fill=with_alpha(colour, point_local),
            )
            canvas.text_block(
                block,
                body_font,
                (int(x0 + pad + theme.scale(0.03)), int(cursor)),
                with_alpha(theme.foreground, point_local),
            )
            cursor += block.height + theme.scale(0.018)
            if cursor > y1 - pad:
                break

    if spec.connector != "none" and eased > 0.55:
        alpha = (eased - 0.55) / 0.45
        cx, cy = theme.width / 2, theme.height / 2
        badge = theme.scale(0.042) * scale
        canvas.circle((cx, cy), badge, fill=theme.background, outline=with_alpha(theme.muted, alpha), width=max(2, theme.scale(0.003)))
        symbol = "vs" if spec.connector == "versus" else "→"
        canvas.text(
            (cx, cy), symbol, theme.font("label"), with_alpha(theme.foreground, alpha), anchor="mm"
        )


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

def draw_map(canvas: Canvas, spec: MapSpec, progress: float) -> None:
    """A schematic locator, not a cartographic basemap.

    LIMITATION, stated plainly: there is no coastline data in this system, so
    this draws a graticule with accurately-placed markers rather than a real
    map. Positions are correct; the land is not drawn. A proper basemap needs a
    tile provider behind a port, which is a Stage 6 adapter, not a fake here.
    """
    theme = canvas.theme
    eased = ease_out_cubic(min(1.0, progress * 1.3))

    frame = (
        theme.margin,
        theme.margin + theme.scale(0.05),
        theme.width - theme.margin,
        theme.height - theme.margin - theme.scale(0.03),
    )
    x0, y0, x1, y1 = frame
    canvas.rounded_rect(
        frame,
        theme.scale(0.02),
        fill=mix(theme.background, theme.foreground, 0.05),
        outline=with_alpha(theme.muted, 0.35),
        width=max(1, theme.scale(0.002)),
    )

    # Graticule.
    for index in range(1, 6):
        x = x0 + (x1 - x0) * index / 6
        canvas.draw.line([(x, y0), (x, y1)], fill=with_alpha(theme.muted, 0.14), width=1)
    for index in range(1, 4):
        y = y0 + (y1 - y0) * index / 4
        canvas.draw.line([(x0, y), (x1, y)], fill=with_alpha(theme.muted, 0.14), width=1)

    # Scope decides the visible window; markers are placed by real coordinates
    # within it, so relative positions are always truthful.
    latitudes = [marker.latitude for marker in spec.markers]
    longitudes = [marker.longitude for marker in spec.markers]
    pad = {"world": 40.0, "continent": 25.0, "country": 12.0, "region": 8.0, "city": 3.0}[spec.scope]
    min_lat, max_lat = min(latitudes) - pad, max(latitudes) + pad
    min_lon, max_lon = min(longitudes) - pad * 1.6, max(longitudes) + pad * 1.6
    min_lat, max_lat = max(-90.0, min_lat), min(90.0, max_lat)
    min_lon, max_lon = max(-180.0, min_lon), min(180.0, max_lon)

    def project(latitude: float, longitude: float) -> tuple[float, float]:
        fx = (longitude - min_lon) / max(1e-6, (max_lon - min_lon))
        fy = 1.0 - (latitude - min_lat) / max(1e-6, (max_lat - min_lat))
        return x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy

    placed = [project(marker.latitude, marker.longitude) for marker in spec.markers]

    if spec.connect_markers and len(placed) > 1:
        for index in range(len(placed) - 1):
            local = stagger(index, len(placed) - 1, eased, overlap=0.5)
            if local <= 0.02:
                continue
            start, end = placed[index], placed[index + 1]
            tip = (start[0] + (end[0] - start[0]) * local, start[1] + (end[1] - start[1]) * local)
            canvas.dashed_line(start, tip, with_alpha(theme.accent, 0.8), width=max(2, theme.scale(0.003)))

    for index, (marker, point) in enumerate(zip(spec.markers, placed, strict=True)):
        local = stagger(index, len(spec.markers), eased, overlap=0.6)
        if local <= 0.02:
            continue
        radius = theme.scale(0.011) * ease_out_back(local)
        canvas.circle(point, radius * 2.6, fill=with_alpha(theme.accent, 0.18 * local))
        canvas.circle(point, radius, fill=theme.accent)
        canvas.text(
            (point[0], point[1] - theme.scale(0.028)),
            marker.label[:28],
            theme.font("label"),
            with_alpha(theme.foreground, local),
            anchor="mm",
        )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Callable[[Canvas, Any, float], None]] = {
    "typography": draw_typography,
    "chart": draw_chart,
    "timeline": draw_timeline,
    "network": draw_network,
    "comparison": draw_comparison,
    "map": draw_map,
}


def draw(canvas: Canvas, spec: object, progress: float) -> None:
    """Draw any animation spec onto a canvas at the given progress."""
    primitive = getattr(spec, "primitive", None)
    key = getattr(primitive, "value", primitive)
    handler = _DISPATCH.get(str(key))
    if handler is None:
        raise ValueError(f"no renderer for visual primitive {key!r}")
    handler(canvas, spec, max(0.0, min(1.0, progress)))


def supported_primitives() -> list[str]:
    return sorted(_DISPATCH)


__all__ = [
    "draw",
    "draw_chart",
    "draw_comparison",
    "draw_map",
    "draw_network",
    "draw_timeline",
    "draw_typography",
    "supported_primitives",
]

"""Conservative column ordering with character preservation and explicit diagnostics."""
from collections import Counter
import re


PARSING_POLICY = 'column-aware-text-v1'
LEGACY_PARSING_POLICY = 'plain-text-v1'


def page_text(page, policy=PARSING_POLICY):
    plain = page.extract_text() or ''
    diagnostic = {'method':'plain','columns_detected':False,'text_preserved':True}
    if policy == LEGACY_PARSING_POLICY:
        return plain, diagnostic
    if policy != PARSING_POLICY:
        raise ValueError('Unknown PDF parsing policy')
    fragments = []
    try:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        def collect(text, cm, tm, _font, size):
            if not text.strip():
                return
            # Transform the text origin into page coordinates, including nested forms.
            x = tm[4]*cm[0]+tm[5]*cm[2]+cm[4]
            y = tm[4]*cm[1]+tm[5]*cm[3]+cm[5]
            if 0 <= x <= width and 0 <= y <= height:
                for offset,line in enumerate(text.splitlines()):
                    if line.strip():
                        fragments.append((x,y-offset*abs(float(size or 10))*1.2,line))
        page.extract_text(visitor_text=collect)
        if len(fragments)<16:
            return plain, {**diagnostic,'reason':'insufficient_positioned_text'}
        left = [f for f in fragments if f[0]<width*.4]
        right = [f for f in fragments if width*.45<f[0]<width*.85]
        if min(len(left),len(right))<8:
            return plain, {**diagnostic,'reason':'no_clear_two_column_structure'}
        split = min(f[0] for f in right)-width*.02
        overlap = min(max(f[1] for f in left),max(f[1] for f in right))-max(min(f[1] for f in left),min(f[1] for f in right))
        if overlap < height*.25:
            return plain, {**diagnostic,'reason':'column_vertical_overlap_unestablished'}
        signals = [f[0]>=split for f in fragments if len(f[2].strip())>=12 and height*.05<f[1]<height*.95]
        transitions = sum(a!=b for a,b in zip(signals,signals[1:]))
        if transitions < 6:
            # Already column-ordered content should not be reconstructed from
            # approximate visitor coordinates. Small floating labels, headers
            # and tables can otherwise create false reading-order repairs.
            return plain, {**diagnostic,'columns_detected':True,
                           'reason':'no_clear_column_interleaving','column_transitions':transitions}
        full = [f for f in fragments if f[0]<split and len(f[2])*5>width*.7 and f[1]>height*.85]
        footer = [f for f in fragments if f[1]<height*.04]
        body = [f for f in fragments if f not in full and f not in footer]
        order = lambda rows: sorted(rows,key=lambda f:(-round(f[1],1),f[0]))
        def lines(rows):
            # Font switches can split a single printed line into many visitor
            # fragments. Preserve those fragments together (including their
            # existing spaces), rather than inserting breaks inside a DOI/name.
            grouped = []
            for fragment in order(rows):
                if grouped and abs(grouped[-1][0][1]-fragment[1]) <= 1:
                    grouped[-1].append(fragment)
                else:
                    grouped.append([fragment])
            def join(row):
                result = ''
                for fragment in sorted(row,key=lambda f:f[0]):
                    text = fragment[2]
                    # pypdf may infer a space between font runs which is absent
                    # from visitor_text. Preserve that original separator; do
                    # not invent/remove word boundaries or split identifiers.
                    if result and not result[-1].isspace() and not text[0].isspace():
                        boundary = re.escape(result[-40:]) + r'\s+' + re.escape(text[:40])
                        if re.search(boundary,plain):
                            result += ' '
                    result += text
                return result
            return [join(row) for row in grouped]
        candidate = '\n'.join(lines(full)+lines([f for f in body if f[0]<split])+
                              lines([f for f in body if f[0]>=split])+lines(footer))
        # Reordering cannot introduce/drop source characters. This says nothing
        # about scientifically correct reading order, which still needs review.
        characters = lambda s: Counter(re.sub(r'\s+','',s))
        if characters(candidate)!=characters(plain):
            return plain, {**diagnostic,'reason':'positioned_text_does_not_preserve_all_characters'}
        return candidate, {'method':'two_column_coordinates','columns_detected':True,
                           'text_preserved':True,'reading_order_verified':False}
    except (AttributeError,TypeError,ValueError,IndexError):
        return plain, {**diagnostic,'reason':'layout_coordinates_unavailable'}

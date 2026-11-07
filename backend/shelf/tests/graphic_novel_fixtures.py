"""Wholly synthetic category breadth, not real catalogue coverage or anyone's taste."""
from copy import deepcopy

STYLES = ('memoir', 'reportage / history', 'literary / slice-of-life',
          'fantasy / SF', 'manga', 'superhero')


def rows():
    result = []
    for index, style in enumerate(STYLES):
        bid = 94001 + index
        result.append({'id': bid, 'title': 'SYNTHETIC ' + style, 'slug': f'synthetic-graphic-{bid}',
            'description': f'SYNTHETIC {style} sequential-art fixture; no provider coverage asserted.',
            'book_category_id': 4, 'canonical_id': None,
            'image': {'url': f'https://covers.example.org/{bid}.jpg', 'width': 600, 'height': 900},
            'book_series': [], 'editions': []})
    for index, position in ((1, 1), (2, 2.5), (3, None), (4, 1)):
        sid = 73001 if index != 4 else 73002
        result[index]['book_series'] = [{'book_id': result[index]['id'], 'series_id': sid,
            'series': {'id': sid, 'name': 'SYNTHETIC shared series name', 'slug': 'synthetic-series'},
            'position': position, 'details': 'SYNTHETIC source membership', 'compilation': index == 3}]
    edition = {'id': 84001, 'book_id': 94001, 'canonical_id': None,
               'edition_format': 'SYNTHETIC paperback', 'edition_information': 'SYNTHETIC edition'}
    result[0]['editions'] = [edition, deepcopy(edition), dict(edition, id=84002)]
    return result

"""Source query selection uses only artist titles after existing disclosure."""
from unittest import TestCase

from shelf.disclosure import catalogue_input, read_json
from .test_disclosure import DisclosureTests, evidence, NOW


class ArtistCatalogueDisclosureTests(TestCase):
    def case(self, *, adapter='musicbrainz_recording'):
        case = DisclosureTests()
        case.setUp()
        case.context.update(purpose='catalogue_lookup', adapter_id=adapter)
        case.grant(['canonical_seed', 'experienced_enjoyment'])
        rows = []
        for number, title, medium in ((10, 'Zed Artist', 'artist'), (11, 'Album Before Artists', 'album'),
                                       (12, 'Alpha Artist', 'artist'), (13, 'Recording Before Artists', 'recording')):
            row = evidence(number, basis='enjoyment', value=4)
            row.update(title=title, medium=medium, purposes=['catalogue_lookup'])
            rows.append(row)
        denied = evidence(14, basis='feedback_note', value={'reaction': 'intrigued', 'text': 'PRIVATE-NOTE'})
        denied.update(title='Denied artist note', medium='artist', purposes=['catalogue_lookup'])
        case.snapshot['evidence'] = rows + [denied]
        return case

    def query(self, case):
        body, _ = catalogue_input(case.snapshot, case.grants, case.context,
                                  ('recording', 'book'), now=NOW)
        return read_json(body)

    def test_recording_query_uses_only_disclosed_artists_and_preserves_other_adapter_behavior(self):
        case = self.case()
        queries = {row['category']: row['terms'] for row in self.query(case)['queries']}
        self.assertEqual(queries['recording'], ['Alpha Artist', 'Zed Artist'])
        self.assertNotIn('Denied artist note', queries['recording'])
        self.assertIn('Album Before Artists', queries['book'])
        ordinary = self.case(adapter='tmdb_movie')
        self.assertIn('Album Before Artists', self.query(ordinary)['queries'][0]['terms'])

    def test_recording_and_album_titles_cannot_substitute_for_missing_artist(self):
        case = self.case()
        case.snapshot['evidence'] = [row for row in case.snapshot['evidence'] if row['medium'] != 'artist']
        self.assertEqual(self.query(case)['queries'][0]['terms'], [])

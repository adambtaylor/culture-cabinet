"""Pure coverage regression specifications; no transport or household fixtures."""
from unittest import TestCase

from shelf.category_registry import CATEGORIES, canonical_category, registry_view
from shelf.enhance_contract import EnhanceFault


class CategoryRegistryTests(TestCase):
    def test_default_registry_keeps_all_groups_and_concrete_unsupported_surfaces(self):
        view = registry_view()
        self.assertEqual({row.group for row in CATEGORIES},
                         {'books', 'films', 'tv', 'music', 'events', 'outings'})
        self.assertTrue({'artist', 'recording', 'podcast_episode', 'gallery',
                         'art_fair', 'outing', 'dining'} <= set(view.categories))
        self.assertNotIn('all', view.categories)
        self.assertEqual(view, registry_view(tuple(reversed(view.categories))))
        self.assertNotEqual(view.revision, registry_view(('movie',)).revision)

    def test_aliases_do_not_merge_media_or_create_an_active_tab_scope(self):
        self.assertEqual(canonical_category('graphic_novel'), 'book')
        self.assertEqual(canonical_category('films'), 'movie')
        self.assertNotEqual(canonical_category('recording'), canonical_category('music'))
        for values in [('all',), ('unknown',), ('book', 'books'), ()]:
            with self.subTest(values=values), self.assertRaises(EnhanceFault):
                registry_view(values)

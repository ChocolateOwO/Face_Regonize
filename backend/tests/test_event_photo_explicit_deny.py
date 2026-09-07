"""Run real MEDIA block in isolation: no application imports, DB or model load."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

import cv2
import numpy as np


SOURCE = Path(__file__).resolve().parents[1] / 'app/services/photo_processing_service.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))


class MediaPolicyTests(unittest.TestCase):
    def render(self, rows, logo=False):
        namespace = {'np': np, 'cv2': cv2}
        helpers = [node for node in TREE.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('_blur_region', '_apply_logo')]
        exec(compile(ast.Module(body=helpers, type_ignores=[]), str(SOURCE), 'exec'), namespace)
        worker = next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                      and node.name == '_run_photo_batch')
        # Extract the contiguous production MEDIA block, including copy, blur,
        # metadata flags and logo call, rather than reimplementing the policy.
        photo_try = next(node for node in ast.walk(worker) if isinstance(node, ast.Try)
                         and any(isinstance(child, ast.Assign)
                                 and any(isinstance(t, ast.Name) and t.id == 'media_img' for t in child.targets)
                                 for child in node.body))
        start = next(i for i, node in enumerate(photo_try.body) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'media_img' for t in node.targets))
        end = next(i for i in range(start, len(photo_try.body))
                   if isinstance(photo_try.body[i], ast.Expr)
                   and isinstance(photo_try.body[i].value, ast.Call)
                   and isinstance(photo_try.body[i].value.func, ast.Name)
                   and photo_try.body[i].value.func.id == '_apply_logo')
        image = np.random.default_rng(2).integers(0, 255, (240, 600, 3), dtype=np.uint8)
        original = image.copy()
        namespace.update(img=image, face_rows=rows, session=SimpleNamespace(add=lambda _: None),
                         logo_config=(np.full((10, 20, 4), 255, np.uint8), 'bottom-right', .1) if logo else None)
        exec(compile(ast.Module(body=photo_try.body[start:end+1], type_ignores=[]), str(SOURCE), 'exec'), namespace)
        np.testing.assert_array_equal(image, original)
        return original, namespace['media_img'], namespace['blurred_count']

    def row(self, identity, consent, offset=0):
        return SimpleNamespace(person_id=identity, consent_status_at_processing=consent,
                               bbox=f'{30+offset},30,{80+offset},80', blurred=False)

    def test_decisions_including_side_profile_match_outcomes(self):
        for identity, consent, blur in [('allow','consented',False), ('deny','declined',True),
                                      (None,'no_match',False), (None,'unknown',False),
                                      (None,'declined',False), ('pending','pending',False),
                                      ('missing',None,False), (None,None,False),
                                      ('matched-side-profile','declined',True)]:
            with self.subTest(identity=identity, consent=consent):
                row = self.row(identity, consent)
                original, media, count = self.render([row])
                self.assertEqual(count, int(blur))
                self.assertEqual(row.blurred, blur)
                self.assertEqual(not np.array_equal(original, media), blur)

    def test_multiface_allow_deny_unknown(self):
        rows = [self.row('a','consented'), self.row('b','declined',180), self.row(None,'no_match',360)]
        original, media, count = self.render(rows)
        self.assertEqual(count, 1)
        self.assertEqual([row.blurred for row in rows], [False, True, False])
        np.testing.assert_array_equal(original[:, :150], media[:, :150])
        np.testing.assert_array_equal(original[:, 350:], media[:, 350:])

    def test_logo_after_blur_and_no_faces(self):
        row = self.row('deny', 'declined')
        row.bbox = '520,195,590,235'
        _, media, count = self.render([row], logo=True)
        self.assertEqual(count, 1)
        self.assertTrue(np.all(media[220:230, 540:580] == 255))
        original, empty_media, count = self.render([])
        np.testing.assert_array_equal(original, empty_media)
        self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()

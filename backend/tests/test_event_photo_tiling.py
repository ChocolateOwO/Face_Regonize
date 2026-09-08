"""Synthetic arrays and fake detector/model only; no DB, photos or GPU loads."""
import ast
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.event_photo_detection_service import collect_candidates, detect_event_faces, _starts


class Detector:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.shapes = []

    def detect(self, image, **kwargs):
        self.shapes.append(image.shape)
        boxes, kps = next(self.responses, ([], None))
        return np.array(boxes, dtype=float).reshape(-1, 5), kps


class TilingTests(unittest.TestCase):
    def run_faces(self, responses, shape=(640, 1088, 3)):
        stats = {}
        detector = Detector(responses)
        faces = collect_candidates(np.zeros(shape, np.uint8), detector, stats)
        return faces, stats, detector

    def test_global_only_preserved(self):
        faces, stats, _ = self.run_faces([([[20, 20, 50, 50, .8]], None)])
        self.assertEqual(len(faces), 1)
        self.assertEqual(stats['global_faces'], 1)

    def test_tile_only_small_face(self):
        faces, stats, detector = self.run_faces([([], None), ([[30, 30, 40, 40, .8]], None)])
        self.assertEqual(len(faces), 1)
        self.assertEqual(stats['global_faces'], 0)
        self.assertEqual(stats['tiles'], 2)
        self.assertEqual(detector.shapes[1:], [(640, 640, 3)] * 2)

    def test_tile_boundary_duplicate(self):
        faces, stats, _ = self.run_faces([([], None), ([[600, 40, 650, 90, .99]], None),
                                        ([[152, 40, 202, 90, .8]], None)])
        self.assertEqual(len(faces), 1)
        self.assertEqual(stats['duplicates_removed'], 1)
        np.testing.assert_array_equal(faces[0].bbox, [600, 40, 650, 90])

    def test_global_tile_duplicate(self):
        faces, stats, _ = self.run_faces([([[50, 50, 90, 90, .7]], None),
                                        ([[50, 50, 90, 90, .9]], None)])
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0].score, .9)
        self.assertEqual(stats['duplicates_removed'], 1)

    def test_bbox_and_landmark_mapping(self):
        kps = np.array([[[12, 12], [18, 12], [15, 15], [12, 18], [18, 18]]])
        faces, _, _ = self.run_faces([([], None), ([], None), ([[10, 10, 20, 20, .9]], kps)])
        np.testing.assert_array_equal(faces[0].bbox, [458, 10, 468, 20])
        np.testing.assert_array_equal(faces[0].kps, kps[0] + [448, 0])
        self.assertEqual(kps[0, 0, 0], 12)

    def test_multiple_nearby_faces_remain_separate(self):
        faces, _, _ = self.run_faces([([[10, 10, 30, 30, .9], [25, 10, 45, 30, .9]], None)])
        self.assertEqual(len(faces), 2)

    def test_deterministic_order(self):
        response = [([[90, 90, 110, 110, .99], [10, 10, 30, 30, .8]], None)]
        first, _, _ = self.run_faces(response)
        second, _, _ = self.run_faces(response)
        self.assertEqual([f.bbox.tolist() for f in first], [f.bbox.tolist() for f in second])
        self.assertEqual(first[0].bbox[0], 10)

    def test_invalid_and_clipped_boxes(self):
        faces, _, _ = self.run_faces([([[-5, -5, 15, 15, .8], [30, 30, 20, 40, .9],
                                      [float('nan'), 0, 10, 10, .9], [2000, 0, 2010, 20, .9]], None)])
        self.assertEqual(len(faces), 1)
        np.testing.assert_array_equal(faces[0].bbox, [0, 0, 15, 15])

    def test_complete_coverage_and_bounded_tiles(self):
        for length in [1, 640, 641, 1088, 6000, 8192]:
            starts = _starts(length)
            self.assertEqual(starts[0], 0)
            self.assertEqual(starts[-1]+min(length, 640), length)
            self.assertTrue(all(b-a <= 448 for a, b in zip(starts, starts[1:])))

    def test_small_image_no_redundant_tile(self):
        _, stats, _ = self.run_faces([([], None)], (300, 300, 3))
        self.assertEqual(stats['tiles'], 0)

    def test_embedding_uses_original_and_mapped_landmarks_once(self):
        image = np.zeros((640, 1088, 3), np.uint8)
        kps = np.full((1, 5, 2), 20, np.float32)
        detector = Detector([([], None), ([], None), ([[10, 10, 30, 30, .9]], kps)])
        seen = []

        def recognize(source, face):
            self.assertIs(source, image)
            seen.append(face.kps.copy())
            face.normed_embedding = np.ones(512, np.float32) / np.sqrt(512)

        engine = ModuleType('app.face_recognition.engine')
        engine.get_face_app = lambda: SimpleNamespace(det_model=detector, models={'recognition': SimpleNamespace(get=recognize)})
        engine.DetectedFace = lambda embedding, bbox, det_score: SimpleNamespace(embedding=embedding, bbox=bbox, det_score=det_score)
        common = ModuleType('insightface.app.common')
        common.Face = SimpleNamespace
        with patch.dict(sys.modules, {'app.face_recognition.engine': engine, 'insightface.app.common': common}):
            result = detect_event_faces(image)
        self.assertEqual(len(seen), 1)
        np.testing.assert_array_equal(seen[0], kps[0]+[448, 0])
        self.assertEqual(result[0].embedding.dtype, np.float32)
        self.assertEqual(result[0].embedding.shape, (512,))
        self.assertAlmostEqual(float(np.linalg.norm(result[0].embedding)), 1, places=6)

    def test_event_worker_wired_to_helper(self):
        source = Path(__file__).resolve().parents[1] / 'app/services/photo_processing_service.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        self.assertTrue(any(isinstance(n, ast.ImportFrom)
                            and n.module == 'app.services.event_photo_detection_service'
                            and any(a.name == 'detect_event_faces' and a.asname == 'detect_faces' for a in n.names)
                            for n in tree.body))


if __name__ == '__main__':
    unittest.main()

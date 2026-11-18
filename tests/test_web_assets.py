"""웹 화면 파일 시험: 외부 파일 무결성, 파일 연결, 서버가 제대로 내려주는지, JS 계산 시험.

    python -m unittest discover -s tests -v
"""
import asyncio
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.webfeed import FeedServer  # noqa: E402

WEB = ROOT / "web"
VENDOR = WEB / "vendor"

# web/vendor/VENDOR.md에 적은 값과 같아야 한다 (Three.js 0.170.0, jsDelivr가 공개한 SHA-256)
VENDOR_FILES = {
    "three.module.min.js": (691648, "CP11RdE9LH+2WraRUwqALa/v1jhZZQGFTyZ9D7E8Oec="),
    "OrbitControls.js": (32134, "gO+q3qT4pjamX7C9CL/vYvPZOgu5Ti51APIxdsXAf04="),
    "THREE-LICENSE.txt": (1081, "TECh72JFC4V8OyqvKUk2MEzVUtll+82dMtTFvPS6RFQ="),
    # 빛 번짐(2026-11-17 추가): examples/jsm 아래의 같은 버전 파일 10개
    "postprocessing/UnrealBloomPass.js": (12410, "O9I6EJevdccALQ/8IabBT0XE3XAduvc3Aw38Yft8ZNk="),
    "postprocessing/EffectComposer.js": (4651, "0jTleGGPqBaVXr3AWcBJxXfiA+ZQ4zzyK94/Iywp5mk="),
    "postprocessing/OutputPass.js": (2524, "Mvh50heQh2dmMceZhXqIVYazzdE7lzG9OxPwZCi9WLc="),
    "postprocessing/MaskPass.js": (2231, "Moz32w2l2b6D/+OdVLAdWsH93xCMyYGC3bsFb1yLU38="),
    "postprocessing/RenderPass.js": (1941, "bJuKU56hbomPZeR2DxSTfvnqlAQ72YQsFB4DAfQZA+g="),
    "postprocessing/Pass.js": (1706, "s8YSg0Dqo35ApqLxtzjolMhVI5QX1QlZdZs0orXon5I="),
    "postprocessing/ShaderPass.js": (1576, "Oyih7ifg65bA6rE3ofRCzPEnqSaQTs7S1R4SXsRK94E="),
    "shaders/OutputShader.js": (1490, "SUTOzUnA1NFSCk2Se96KWQ/UPwQe6RMlK5RRhVoB0PA="),
    "shaders/LuminosityHighPassShader.js": (1147, "n0hm+auy2W/YPuxGukvyFlsiFVp6N/9CXA9g66GAB8s="),
    "shaders/CopyShader.js": (571, "TjNG2xlNtWpZbNB06b2zn7XrUgQMMz4NKdxOsTJNOx0="),
}


def sha256_b64(path: Path) -> str:
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode()


# 코드에서 HTML을 직접 넣는 형태. 주석에 단어가 적힌 것은 걸리지 않도록 '사용 형태'만 찾는다.
HTML_INJECTION = re.compile(r"\.\s*innerHTML\b|\.\s*outerHTML\b|insertAdjacentHTML\s*\(|document\s*\.\s*write\s*\(")


def uses_html_injection(js_text: str) -> bool:
    return HTML_INJECTION.search(js_text) is not None


class VendorIntegrityTest(unittest.TestCase):
    def test_files_match_recorded_size_and_hash(self):
        """외부에서 가져온 파일이 한 바이트라도 바뀌면 이 시험이 실패한다 (공급망 점검)."""
        for name, (size, digest) in VENDOR_FILES.items():
            with self.subTest(name):
                path = VENDOR / name
                self.assertTrue(path.is_file(), f"{name} 파일이 없습니다")
                self.assertEqual(path.stat().st_size, size)
                self.assertEqual(sha256_b64(path), digest)

    def test_vendor_doc_records_the_same_hashes(self):
        doc = (VENDOR / "VENDOR.md").read_text(encoding="utf-8")
        for name, (_, digest) in VENDOR_FILES.items():
            with self.subTest(name):
                self.assertIn(digest, doc, "VENDOR.md의 해시가 시험과 다릅니다")
                self.assertIn(name, doc)
        self.assertIn("0.170.0", doc)
        self.assertIn("MIT", doc)

    def test_license_is_kept_with_the_library(self):
        text = (VENDOR / "THREE-LICENSE.txt").read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("three.js authors", text.lower())           # 실제 파일에는 소문자로 적혀 있다
        self.assertIn("Permission is hereby granted", text)

    def test_library_reports_expected_version(self):
        head = (VENDOR / "three.module.min.js").read_bytes()[:400].decode("utf-8", "replace")
        self.assertIn('const t="170"', head)
        self.assertIn("SPDX-License-Identifier: MIT", head)


class HtmlWiringTest(unittest.TestCase):
    def setUp(self):
        self.html = (WEB / "index.html").read_text(encoding="utf-8")

    def test_importmap_maps_three_to_an_existing_file(self):
        match = re.search(r'<script type="importmap">(.*?)</script>', self.html, re.S)
        self.assertIsNotNone(match, "importmap이 없습니다")
        imports = json.loads(match.group(1))["imports"]
        self.assertIn("three", imports)
        self.assertTrue((WEB / imports["three"]).is_file(), f"{imports['three']} 파일이 없습니다")

    def test_importmap_comes_before_module_scripts(self):
        """importmap은 모듈 스크립트보다 앞에 있어야 브라우저가 이름을 풀 수 있다."""
        self.assertLess(self.html.index('type="importmap"'), self.html.index('type="module"'))

    def test_app_is_loaded_as_a_module(self):
        self.assertRegex(self.html, r'<script type="module" src="app\.js">')

    def test_local_references_in_html_exist(self):
        for ref in re.findall(r'(?:src|href)="([^"#]+)"', self.html):
            if re.match(r"^(https?:)?//", ref):
                self.fail(f"외부 주소를 쓰고 있습니다 (오프라인에서 동작하지 않음): {ref}")
            with self.subTest(ref):
                self.assertTrue((WEB / ref).is_file(), f"{ref} 파일이 없습니다")

    def test_dom_ids_used_by_app_js_exist_in_html(self):
        app = (WEB / "app.js").read_text(encoding="utf-8")
        for dom_id in re.findall(r'getElementById\("([^"]+)"\)', app):
            with self.subTest(dom_id):
                self.assertIn(f'id="{dom_id}"', self.html, f"app.js가 쓰는 id={dom_id}가 index.html에 없습니다")

    def test_js_imports_resolve(self):
        """상대 경로 import는 실제 파일이어야 하고, 'three' 같은 이름 import는 importmap에 있어야 한다."""
        mapped = {"three"}
        for js in (WEB / "app.js", WEB / "scene.js", WEB / "robot_math.js", WEB / "agv_logic.js",
                   WEB / "factory_logic.js", WEB / "factory_env.js", WEB / "bloom.js", VENDOR / "OrbitControls.js",
                   *sorted((VENDOR / "postprocessing").glob("*.js")), *sorted((VENDOR / "shaders").glob("*.js"))):
            text = js.read_text(encoding="utf-8")
            specifiers = re.findall(r'''(?:from\s+|import\()\s*["']([^"']+)["']''', text)
            for spec in specifiers:
                with self.subTest(file=js.name, spec=spec):
                    if spec.startswith("."):
                        self.assertTrue((js.parent / spec).is_file(), f"{js.name}이 불러오는 {spec}가 없습니다")
                    else:
                        self.assertIn(spec, mapped, f"{js.name}의 '{spec}'가 importmap에 없습니다")

    def test_bloom_import_failure_is_handled(self):
        """빛 번짐 파일을 불러오지 못해도 3D 화면은 동작해야 한다 (import().catch가 있어야 함)."""
        scene = (WEB / "scene.js").read_text(encoding="utf-8")
        self.assertRegex(scene, r'import\("\./bloom\.js"\)[\s\S]*?\.catch\(')

    def test_bloom_toggle_is_wired_in_html(self):
        """빛 번짐 스위치는 처음에는 눌러지지 않고(파일을 불러온 뒤에만 켜짐) index.html에 있어야 한다."""
        self.assertRegex(self.html, r'id="bloom-toggle"[^>]*disabled')

    def test_dynamic_import_failure_is_handled(self):
        """3D를 불러오지 못해도 나머지 화면이 동작하도록 import().catch가 있어야 한다."""
        app = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertRegex(app, r'import\("\./scene\.js"\)[\s\S]*?\.catch\(')

    def test_text_is_never_inserted_as_html(self):
        """서버 데이터를 innerHTML로 넣으면 안 된다 (섞인 태그가 실행될 수 있음)."""
        for js in (WEB / "app.js", WEB / "scene.js", WEB / "agv_logic.js", WEB / "factory_logic.js", WEB / "factory_env.js", WEB / "bloom.js"):
            with self.subTest(js.name):
                self.assertFalse(uses_html_injection(js.read_text(encoding="utf-8")),
                                 f"{js.name}에서 HTML을 직접 넣는 코드가 있습니다")

    def test_html_injection_check_itself_works(self):
        """위 검사가 진짜 위험한 코드는 잡고, 설명 주석은 통과시키는지 확인한다."""
        for bad in ('box.innerHTML = data;', 'box . innerHTML += x', 'a.outerHTML = x',
                    'el.insertAdjacentHTML("beforeend", x)', 'document.write(x)'):
            with self.subTest(bad):
                self.assertTrue(uses_html_injection(bad))
        for good in ('// innerHTML에 서버 데이터를 넣으면 위험하다', 'node.textContent = data;',
                     'const note = "innerHTML을 쓰지 않는다";', 'el.append(document.createTextNode(x))'):
            with self.subTest(good):
                self.assertFalse(uses_html_injection(good))


class ServedAssetsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FeedServer(WEB, "127.0.0.1", 0)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)

    async def get(self, path):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.port}\r\n\r\n".encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5.0)
        writer.close()
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        return int(lines[0].split(" ")[1]), headers, body

    async def test_every_script_is_served_as_javascript(self):
        """모듈 스크립트는 MIME 형식이 JavaScript가 아니면 브라우저가 실행하지 않는다."""
        for path, file in [("/app.js", WEB / "app.js"), ("/scene.js", WEB / "scene.js"),
                           ("/robot_math.js", WEB / "robot_math.js"), ("/agv_logic.js", WEB / "agv_logic.js"),
                           ("/factory_logic.js", WEB / "factory_logic.js"), ("/factory_env.js", WEB / "factory_env.js"),
                           ("/bloom.js", WEB / "bloom.js"),
                           ("/vendor/postprocessing/UnrealBloomPass.js", VENDOR / "postprocessing" / "UnrealBloomPass.js"),
                           ("/vendor/shaders/CopyShader.js", VENDOR / "shaders" / "CopyShader.js"),
                           ("/vendor/three.module.min.js", VENDOR / "three.module.min.js"),
                           ("/vendor/OrbitControls.js", VENDOR / "OrbitControls.js")]:
            with self.subTest(path):
                status, headers, body = await self.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith("text/javascript"), headers["content-type"])
                self.assertEqual(body, file.read_bytes())            # 한 바이트도 바뀌지 않고 전달된다

    async def test_page_and_style_are_served(self):
        status, headers, body = await self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"importmap", body)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        status, headers, _ = await self.get("/style.css")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/css"))

    async def test_license_is_downloadable_with_the_library(self):
        status, headers, body = await self.get("/vendor/THREE-LICENSE.txt")
        self.assertEqual(status, 200)
        self.assertIn(b"MIT License", body)


@unittest.skipUnless(shutil.which("node"), "Node.js가 없어서 JS 시험을 건너뜁니다")
class NodeJsTest(unittest.TestCase):
    def test_robot_math_js_tests_pass(self):
        """web/robot_math.js, web/agv_logic.js의 계산 시험(tests/js)을 Node.js로 실행한다."""
        result = subprocess.run(
            [shutil.which("node"), "--test", "tests/js/robot_math.test.mjs", "tests/js/agv_logic.test.mjs", "tests/js/factory_logic.test.mjs"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"fail 0")
        self.assertRegex(result.stdout, r"tests [1-9]")                # 시험이 실제로 실행되었다


if __name__ == "__main__":
    unittest.main()

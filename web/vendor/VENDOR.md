# 외부에서 가져온 파일 (직접 만든 코드가 아님)

이 폴더의 파일은 Three.js 라이브러리를 **그대로** 가져온 것입니다. 수정하지 않았습니다.

| 파일 | 크기(바이트) | SHA-256 (base64) |
|---|---|---|
| `three.module.min.js` | 691,648 | `CP11RdE9LH+2WraRUwqALa/v1jhZZQGFTyZ9D7E8Oec=` |
| `OrbitControls.js` | 32,134 | `gO+q3qT4pjamX7C9CL/vYvPZOgu5Ti51APIxdsXAf04=` |
| `THREE-LICENSE.txt` | 1,081 | `TECh72JFC4V8OyqvKUk2MEzVUtll+82dMtTFvPS6RFQ=` |
| `postprocessing/UnrealBloomPass.js` | 12,410 | `O9I6EJevdccALQ/8IabBT0XE3XAduvc3Aw38Yft8ZNk=` |
| `postprocessing/EffectComposer.js` | 4,651 | `0jTleGGPqBaVXr3AWcBJxXfiA+ZQ4zzyK94/Iywp5mk=` |
| `postprocessing/OutputPass.js` | 2,524 | `Mvh50heQh2dmMceZhXqIVYazzdE7lzG9OxPwZCi9WLc=` |
| `postprocessing/MaskPass.js` | 2,231 | `Moz32w2l2b6D/+OdVLAdWsH93xCMyYGC3bsFb1yLU38=` |
| `postprocessing/RenderPass.js` | 1,941 | `bJuKU56hbomPZeR2DxSTfvnqlAQ72YQsFB4DAfQZA+g=` |
| `postprocessing/Pass.js` | 1,706 | `s8YSg0Dqo35ApqLxtzjolMhVI5QX1QlZdZs0orXon5I=` |
| `postprocessing/ShaderPass.js` | 1,576 | `Oyih7ifg65bA6rE3ofRCzPEnqSaQTs7S1R4SXsRK94E=` |
| `shaders/OutputShader.js` | 1,490 | `SUTOzUnA1NFSCk2Se96KWQ/UPwQe6RMlK5RRhVoB0PA=` |
| `shaders/LuminosityHighPassShader.js` | 1,147 | `n0hm+auy2W/YPuxGukvyFlsiFVp6N/9CXA9g66GAB8s=` |
| `shaders/CopyShader.js` | 571 | `TjNG2xlNtWpZbNB06b2zn7XrUgQMMz4NKdxOsTJNOx0=` |

- 이름: Three.js
- 버전: 0.170.0 (npm 패키지 `three`, 2024-10-31 게시)
- 라이선스: MIT, Copyright 2010-2024 Three.js Authors (전문은 `THREE-LICENSE.txt`)
- 홈페이지: https://threejs.org/
- 받은 곳: https://cdn.jsdelivr.net/npm/three@0.170.0/ 아래의 아래 경로
  - `build/three.module.min.js`
  - `examples/jsm/controls/OrbitControls.js`
  - `LICENSE` (이 폴더에서는 `THREE-LICENSE.txt`로 저장)
- 받은 날짜: 2026-11-16 (본체, OrbitControls, 라이선스)
- 빛 번짐용 추가 파일 10개 (`postprocessing/`, `shaders/` 폴더): 2026-11-17에 사용자의 명시적 허락을 받고 받았습니다.
  받은 곳은 위와 같은 `https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/` 아래이고, 합계 30,247바이트입니다.
  파일끼리 `./Pass.js`, `../shaders/CopyShader.js`처럼 상대 경로로 서로 불러오므로 폴더 구조를 바꾸면 안 됩니다.
  이 파일들에는 외부 주소로 접속하는 코드가 없습니다(주석에 적힌 주소뿐). 쓰는 곳은 `web/bloom.js`입니다.

## 어떻게 믿을 수 있는지 확인했나
1. jsDelivr가 공개한 파일 해시와 받은 파일의 SHA-256이 일치했습니다 (빛 번짐 파일 10개도 크기와 해시가 모두 일치).
2. 다른 미러(unpkg.com)가 주는 파일과도 같았습니다 (두 곳이 같은 파일을 줌).
3. `tests/test_web_assets.py`가 이 파일들의 크기와 해시를 매번 확인합니다. 파일이 바뀌면 시험이 실패합니다.

## 다시 확인하거나 바꾸려면
- 해시 확인: 파일의 SHA-256을 계산해 base64로 바꾼 값이 위 표와 같은지 봅니다.
- 버전을 바꿀 때: 새 파일을 임시 폴더에 받아 해시를 공개된 값과 비교한 뒤 이 폴더에 넣고,
  이 문서의 표와 `tests/test_web_assets.py`의 기대 해시를 같이 고칩니다.

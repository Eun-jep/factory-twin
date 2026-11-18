// 빛 번짐(Bloom): 아주 밝은 부분(경광등, 조명판, 모니터 테두리)이 주변으로 은은하게 번져 보이게 하는 후처리 효과.
//
// 원리: 장면을 바로 화면에 그리지 않고 한 번 거쳐 간다.
//   1. RenderPass       장면을 그림(메모리 위의 그림판에)
//   2. UnrealBloomPass  그림에서 밝은 부분만 골라 흐리게 퍼뜨린 뒤 원래 그림에 더함
//   3. OutputPass       색을 화면용으로 되돌림
// Three.js가 제공하는 파일(web/vendor/postprocessing, web/vendor/shaders)을 그대로 쓰고, 여기서는 이어 붙이기만 한다.
//
// 이 파일은 scene.js가 import()로 따로 불러온다. 그래서 여기서 문제가 생기거나(파일 없음, 그래픽 미지원)
// 불러오지 못해도 빛 번짐만 빠지고 나머지 화면은 그대로 동작한다.

import * as THREE from "three";
import { EffectComposer } from "./vendor/postprocessing/EffectComposer.js";
import { RenderPass } from "./vendor/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "./vendor/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "./vendor/postprocessing/OutputPass.js";

// 세기(strength), 퍼짐(radius), 기준 밝기(threshold: 이보다 밝은 부분만 번진다)
export const BLOOM_SETTINGS = { strength: 0.55, radius: 0.5, threshold: 1.15 };

export function createBloom(renderer, scene, camera, width, height) {
  // 후처리용 그림판은 기본으로는 계단 현상 방지(MSAA)가 꺼져 있어서 4배로 켠다
  const target = new THREE.WebGLRenderTarget(width, height, { type: THREE.HalfFloatType, samples: 4 });
  const composer = new EffectComposer(renderer, target);
  composer.setPixelRatio(renderer.getPixelRatio());
  composer.setSize(width, height);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(width, height), BLOOM_SETTINGS.strength, BLOOM_SETTINGS.radius, BLOOM_SETTINGS.threshold);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());
  return {
    render: () => composer.render(),
    setSize: (w, h) => composer.setSize(w, h),
  };
}

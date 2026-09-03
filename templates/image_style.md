<!--
배경 이미지 생성(04, IMAGE_PROVIDER=fal)과 대본(01)이 함께 쓰는 고정 문구.
- ## style  : 04 가 모든 프롬프트 끝에 그대로 붙이는 그림체·구도 문구. 긍정형 표현만 쓴다.
              ("~없이", "no ~" 같은 부정 표현은 생성 모델이 오히려 그 대상을 그리는 경우가 있어 쓰지 않는다)
- ## avoid  : 01 검증기의 image_prompt 금지어 목록으로만 쓴다. 프롬프트에는 붙이지 않는다.
              쉼표로 나눈 단어·짧은 구절. 단수·복수를 함께 잡는다 (face → face, faces).
- ## rules  : 01 이 대본 프롬프트에 넣는 한국어 규칙. 문장별 image_prompt 를 쓸 때 지킬 것.
-->

## style
soft warm illustration, gentle flat colors, calm and friendly mood, simple composition with one clear subject, vertical 9:16 portrait, main subject placed in the upper two thirds of the frame, clean open space of plain color across the lower third, everyday Korean home and neighborhood scenery, people shown from behind or at a distance, high quality

## avoid
text, letter, number, word, sign, label, logo, watermark, caption, close-up, closeup, face, facial, portrait of a person, medical device, pill, tablet, syringe, needle, blood, monitor, stethoscope, thermometer, blood pressure cuff, hospital bed, dark mood, scary

## rules
- 영어 한 문장, 장면만 묘사합니다. 그림체·색감·화질 같은 스타일 말은 쓰지 않습니다 (따로 붙습니다).
- 사람은 뒷모습·옆모습·멀리서 보이거나 손만 나오게 합니다. 얼굴 클로즈업, 얼굴 생김새 묘사는 금지입니다.
- 글자·숫자·간판·라벨·달력 숫자가 보이는 장면은 금지입니다.
- 약, 주사기, 혈압계 같은 의료기기의 세부 묘사는 금지입니다. 필요하면 "outside a small clinic building" 정도로 멀리서 그립니다.
- 부정 표현("without text", "no face")을 쓰지 말고, 보여 줄 것만 씁니다.
- 문장 하나에 장면 하나. 그 문장의 뜻이 그림만 봐도 짐작되게 합니다.
- 예: "an elderly person seen from behind, holding a glass of water by a sunny window"

# KT 통화매니저 모바일 전화 연결 검토

확인일: 2026-11-07. 대상: Android Chrome/PWA와 iPhone Safari/PWA. 공개된 KT·Google·Apple 공식 자료만 검토했다. 앱 설치·실행, 가입·로그인, 발신, 계정·회선 설정 변경은 수행하지 않았다.

## 현재 판단

**Android와 iPhone 모두 KT 공식 통화매니저 앱은 확인했다. 그러나 외부 웹의 전화 버튼에서 상대 번호를 넘기는 공식 URI·Intent·iOS URL scheme 규격은 공개 자료에서 확인하지 못했다.** 따라서 현재 근거로 번호 전달 기능을 구현 완료하거나 지원된다고 판단할 수 없다. 이는 기능이 없다는 확정 판단이 아니라 공개 규격과 실기기 검증이 부족하다는 뜻이다.

현재 모바일 소스의 `web/core.js` 전화 경로는 `DIAL_INTENT` 기록 후 `tel:` 링크를 연다. 수정 번호 전화 경로도 `web/screens/stop.js`에서 `tel:`을 사용한다. 이번 검토에서는 모바일 소스를 변경하지 않았다.

## 지원 앱과 확인된 기능

- Android: [Google Play의 통화매니저](https://play.google.com/store/apps/details?id=com.byto.olleh.biz&hl=ko), 개발자 **KT Corporation**, 패키지 **`com.byto.olleh.biz`**. 공식 앱 설명에는 KT 유선전화 관리와 앱 내부 터치콜 기능이 있다. 외부 앱이나 웹페이지에서 전화번호를 받는 공개 호출 규격은 이 설명에 없다.
- iPhone: [App Store의 통화매니저](https://apps.apple.com/kr/app/%ED%86%B5%ED%99%94%EB%A7%A4%EB%8B%88%EC%A0%80/id983920725), 개발자 **KT Corporation**, 앱 ID **`983920725`**. 공식 설명에 앱 내부 터치콜이 있고 확인 당시 iOS 15.6 이상을 요구한다. 공개 설명에서 URL scheme·Universal Link·번호 전달 매개변수는 확인되지 않았다.
- [KT 통화매니저 APP 상품 안내](https://product.kt.com/wDic/productDetail.do?ItemCode=985)는 별도의 KT 상품이다. 앱 배포 페이지의 존재만으로 사용자의 회선 가입 상태나 발신 가능 여부를 확인할 수 없다.

## OS별 결과표

| OS | 단순 실행 | 전화번호 넘기기 | 연동이 없거나 앱이 없을 때 대안 | 현재 확인 한계 | KT에 필요한 규격 질문 |
| --- | --- | --- | --- | --- | --- |
| Android Chrome/PWA | 설치한 앱을 홈 화면에서 직접 여는 수동 경로를 사용할 수 있다. 웹 버튼에서 여는 공식 URI는 미확인이다. | 미확인. 패키지 이름만으로 대상 Activity의 외부 호출·번호 해석 지원을 입증할 수 없다. | 번호를 수동 복사 → 사용자가 KT 앱을 직접 열기 → 번호 입력·붙여넣기 후 확인. 미설치라면 공식 Google Play 안내를 제공하는 방안. | `BROWSABLE` Activity, action/data URI/extras 및 지원 버전 미확인. 기기에서 실행하지 않았다. | 브라우저에 공개된 Intent filter와 정확한 URI·번호 필드가 있는가? 번호 입력 화면만 열 수 있는가? |
| iPhone Safari/PWA | 설치한 앱의 홈 화면 수동 실행 경로를 사용할 수 있다. 웹에서 실행하는 KT 전용 scheme·Universal Link는 미확인이다. | 미확인. App Store ID는 번호 전달 규격이 아니다. KT 앱의 기본 통화앱 지원 여부도 확인되지 않았다. | 번호를 수동 복사 → 홈 화면에서 KT 앱 직접 열기 → 번호 입력·붙여넣기 후 확인. 미설치라면 공식 App Store 안내를 제공하는 방안. | 등록된 scheme·공식 URL 형식, 기본 통화앱 지원, Safari와 홈 화면 PWA 차이 미확인. 실기기 실행하지 않았다. | 공식 scheme 또는 Universal Link가 있는가? 번호 매개변수·지원 iOS 버전·기본 통화앱 지원 조건은 무엇인가? |

위 수동 경로는 자동 연결 기능을 대신하는 운영 제안이다. KT 앱에서의 붙여넣기 위치, 로그인 유지, 회선 선택, 발신 전 확인 화면은 직접 시험하지 않았다. 스토어 링크는 앱 안내 페이지를 여는 용도이며 설치된 KT 앱으로 번호를 전달하는 링크로 취급하지 않는다.

## 플랫폼 규격이 뜻하는 범위

### Android

[Chrome 공식 Intent 문서](https://developer.chrome.com/docs/android/intents)는 웹에서 앱을 열 때 사용자 동작이 필요하며, 이 방식으로 호출할 수 있는 Activity에 `android.intent.category.BROWSABLE`이 있어야 한다고 설명한다. 대상 앱이 Intent를 처리하지 못하면 실행되지 않으며, 문서화된 웹 fallback 주소를 둘 수 있다.

따라서 `package=com.byto.olleh.biz`를 안다는 사실만으로 KT 앱 실행이나 번호 전달이 보장되지는 않는다. KT가 지원하는 action·scheme·host/path·extras와 해당 Activity의 수신 처리를 확인해야 한다. 이 문서에는 추측한 KT Intent 문자열을 제시하지 않는다.

[Android 공식 전화 Intent 안내](https://developer.android.com/guide/components/intents-common#Phone)는 일반 `ACTION_DIAL`과 `tel:` 번호 표시를 설명한다. 이는 일반 전화 앱 처리 규격이며 KT 통화매니저가 그 Intent를 수신한다는 근거가 아니다.

### iPhone

[Apple의 custom URL scheme 안내](https://developer.apple.com/documentation/xcode/defining-a-custom-url-scheme-for-your-app)는 대상 앱이 scheme을 등록하고 전달된 URL을 해석해야 한다고 설명한다. 앱 이름이나 App Store ID에서 KT 전용 scheme을 유추할 수 없다.

[Apple의 기본 통화앱 개발 안내](https://developer.apple.com/documentation/callkit/preparing-your-app-to-be-the-default-calling-app)에 따르면 iOS/iPadOS 18.2 이상에서는 지원하는 기본 통화앱이 `tel:` URL을 처리할 수 있다. 따라서 `tel:`이 항상 특정 Apple 앱만 연다고 단정해서도 안 된다. 다만 현재 자료로는 KT 통화매니저가 이 기능을 지원하거나 사용자 기기에서 기본 통화앱으로 설정 가능한지 확인되지 않았다. `tel:` 링크 자체가 KT 앱과 KT 유선 발신을 지정하는 계약은 아니다.

네이티브 앱의 [UIApplication.canOpenURL](https://developer.apple.com/documentation/uikit/uiapplication/canopenurl(_:))은 등록된 URL 처리 가능성을 확인하는 API다. 이를 브라우저 PWA가 그대로 호출할 수 있는 설치 확인 기능으로 취급하지 않는다. 공식 KT URL이 확인되더라도 실제 Safari/PWA 동작과 미설치 처리는 별도 확인이 필요하다.

## KT API와 제휴상품에서 확인한 점

[KT 통화매니저 API 서비스 소개](https://dev.fone.kt.com/useInfo/serviceInfo.dev)는 전화걸기·발신번호·착신전환 등의 연동 기능을 소개한다. [KT 이용자 가이드](https://dev.fone.kt.com/useInfo/userGuide.dev)는 개발자 등록, 인증키, SDK 및 규격문서 제공 절차를 안내한다. 그러나 이 공개 소개는 통화매니저 모바일 앱의 딥링크 규격을 제공하지 않는다.

확인 당시 [SDK 다운로드](https://dev.fone.kt.com/registration/sdkDownload.dev)와 [FAQ](https://dev.fone.kt.com/customer/faq.dev)는 로그인 필요 안내로 이동했다. 가입이나 로그인을 하지 않았으므로 그 안의 규격·지원 OS·제휴 조건은 확인하지 않았다.

[KT 시장특화 솔루션 소개](https://dev.fone.kt.com/service_info.dev)에는 지자체·관공서 대상 공공매니저가 제휴 CRM과 통화매니저 APP의 3자통화를 활용하는 상품으로 소개되어 있다. 이는 KT에 문의할 공식 연동 경로가 있다는 근거다. 해당 제휴상품의 존재만으로 이 폐기물 PWA가 일반 앱 링크를 통해 같은 기능을 사용할 수 있다고 판단하지 않는다.

## KT에 확인해야 할 질문

1. Android/iPhone 각각 일반 외부 웹과 홈 화면 PWA에서 지원하는 **공식 앱 실행 URL** 및 **전화번호 전달 URL**이 있는가? 공개 가능한 최신 규격서와 최소 지원 버전을 받을 수 있는가?
2. Android는 정확한 action·data URI·scheme·host/path·extras 이름/타입, `BROWSABLE` 지원 Activity, 지원 브라우저를 확인해 달라. 앱의 단순 실행과 번호가 채워진 화면 실행을 구분해 달라.
3. iPhone은 정확한 custom scheme 또는 Universal Link 형식과 번호 매개변수를 확인해 달라. iOS 기본 통화앱 기능을 지원하는지, 국내 사용과 Safari/PWA에서 어떤 조건이 필요한지도 확인해 달라.
4. 번호 형식은 국내 앞자리 `0`, `+82`, 하이픈, URL 인코딩 중 무엇을 허용하는가? 번호 전달은 입력 화면만 여는지, 회신·3자통화·발신을 즉시 시작하는지도 구분해 달라.
5. 발신번호가 기관의 KT 유선번호로 표시되려면 어떤 회선·상품·계정 권한이 필요한가? 통화매니저 APP, API형, 공공매니저 중 이 사용 방식에 맞는 상품과 인증 방식은 무엇인가?
6. 앱 미설치·로그아웃·여러 회선·연결 실패 때 공식 fallback과 오류 계약이 있는가? 개인정보를 URL에 전달할 때 허용 범위와 보관 조건은 무엇인가?

## 적용 범위와 후속 판단

이번 결과는 공개 자료 조사 완료이며 모바일 동작 변경 완료가 아니다. KT 규격을 확보하기 전에는 알려지지 않은 scheme이나 전화번호 매개변수를 만들어 배포하지 않는다. 현재 가능한 제안 범위는 번호 수동 복사와 사용자의 KT 앱 직접 실행 안내다.

공식 규격을 받은 뒤 Android Chrome·설치형 PWA와 iPhone Safari·설치형 PWA에서 앱 설치/미설치, 로그인/로그아웃, 번호 앞자리 보존, 사용자 확인 전 발신 여부를 가상 시험 자료와 허가된 시험 회선으로 검증해야 번호 전달 지원을 확정할 수 있다. 이번 조사에서는 그러한 실행·발신 검증을 수행하지 않았다.

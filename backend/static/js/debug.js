"use strict";

/**
 * 개발용 로그 설정
 * true  : 개발 로그 표시
 * false : 운영 로그 숨김
 */
const DEBUG = false;

function debugLog(...args) {
    if (DEBUG) {
        console.log(...args);
    }
}
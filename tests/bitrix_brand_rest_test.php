<?php
/** Contract tests with Bitrix/DB doubles. Run: php tests/bitrix_brand_rest_test.php */
namespace Bitrix\Rest {
    class RestException extends \RuntimeException {
        public function __construct($message, $code = '') { parent::__construct($message); }
    }
}
namespace Bitrix\Main {
    class Loader { public static function includeModule($name) { return true; } }
    class Application { public static function getConnection() { return $GLOBALS['db']; } }
}
namespace {
    // WASM test runtime lacks intl. This deliberately supports ASCII only; it does NOT
    // test Unicode normalization or replace the required production intl extension.
    if (!class_exists('Normalizer')) {
        class Normalizer {
            const FORM_KC = 5;
            public static function normalize($value, $form) {
                if (preg_match('/[^\x00-\x7f]/', $value)) { throw new \RuntimeException('ASCII_ONLY_TEST_SHIM'); }
                return $value;
            }
        }
    }
    class Rows {
        private $rows;
        public function __construct($rows) { $this->rows = array_values($rows); }
        public function Fetch() { return array_shift($this->rows); }
    }
    class User {
        public $id = 7;
        public function IsAuthorized() { return $this->id > 0; }
        public function GetID() { return $this->id; }
    }
    class Db {
        public $busy = false;
        public $locked = false;
        public function query($sql) {
            if (strpos($sql, 'GET_LOCK') !== false) {
                $this->locked = !$this->busy;
                return new Rows([['L' => $this->locked ? 1 : 0]]);
            }
            $this->locked = false;
            return new Rows([['L' => 1]]);
        }
    }
    class CUtil { public static function translit($v, $lang, $opts) { return str_replace(' ', '-', strtolower($v)); } }
    class CIBlockProperty {
        public static $link = 12;
        public static function GetByID($id, $iblock) {
            return new Rows([['IBLOCK_ID' => 14, 'CODE' => 'BRAND', 'PROPERTY_TYPE' => 'E', 'LINK_IBLOCK_ID' => self::$link]]);
        }
    }
    class CIBlockSectionRights { public static function UserHasRightTo($a, $b, $c) { return true; } }
    class CIBlockElementRights {
        public static $denied = [];
        public static function UserHasRightTo($iblock, $id, $right) { return !in_array($id, self::$denied, true); }
    }
    class CIBlockElement {
        public static $rows = [];
        public static $adds = 0;
        public static $failAdd = false;
        public static $badReadback = false;
        public static function GetList($sort, $filter, $group, $nav, $select) {
            return new Rows(array_slice(self::$rows, 0, $nav['nTopCount']));
        }
        public static function GetByID($id) {
            $row = self::$rows[$id] ?? null;
            if ($row && self::$badReadback) { $row['IBLOCK_ID'] = 99; }
            return new Rows($row ? [$row] : []);
        }
        public function Add($fields) {
            check($GLOBALS['db']->locked, 'add executes under dictionary lock');
            if (self::$failAdd) { return false; }
            $id = 100 + count(self::$rows);
            self::$adds++;
            self::$rows[$id] = ['ID' => $id] + $fields;
            return $id;
        }
    }
    define('B_PROLOG_INCLUDED', true);
    require __DIR__ . '/../integrations/bitrix/server/panda_brand_rest.php';
    $USER = new User(); $db = new Db(); $checks = 0;
    PandaBrandRest::configure(['allowed_user_ids' => [7], 'brand_iblock_id' => 12,
        'catalog_iblock_id' => 14, 'brand_property_id' => 100]);
    function check($condition, $message) {
        $GLOBALS['checks']++;
        if (!$condition) { throw new \RuntimeException($message); }
    }
    function expectError($code, $call) {
        try { $call(); } catch (\Bitrix\Rest\RestException $e) {
            check($e->getMessage() === $code, 'expected ' . $code . ', got ' . $e->getMessage()); return;
        }
        throw new \RuntimeException('missing error ' . $code);
    }
    function writePlan($plan) { return $plan + ['idempotency_key' => 'brand:stable-key']; }
    $plan = PandaBrandRest::preview(['name' => ' TCL ']);
    check($plan['action'] === 'create' && $plan['code'] === 'tcl', 'preview new brand');
    check(CIBlockElement::$adds === 0, 'preview never writes');
    $result = PandaBrandRest::resolve(writePlan($plan));
    check($result['created'] && $result['brand_id'] === '100', 'create and read back ID');
    check(!$db->locked, 'success releases lock');
    $replay = PandaBrandRest::resolve(writePlan($plan));
    check(!$replay['created'] && CIBlockElement::$adds === 1, 'lost response/retry cannot duplicate');
    $same = PandaBrandRest::preview(['name' => 'tCl']);
    check($same['brand_id'] === '100', 'case-insensitive exact name');
    $again = PandaBrandRest::resolve(writePlan($same));
    check(!$again['created'], 'existing does not write');
    expectError('PANDA_BRAND_ID_MISMATCH', function () { PandaBrandRest::preview(['name' => 'TCL', 'expected_id' => '999']); });
    CIBlockElement::$rows[200] = ['ID' => 200, 'IBLOCK_ID' => 12, 'NAME' => ' TCL ', 'CODE' => 'tcl-copy', 'XML_ID' => ''];
    expectError('PANDA_BRAND_AMBIGUOUS', function () { PandaBrandRest::resolve(writePlan(PandaBrandRest::preview(['name' => 'TCL']))); });
    unset(CIBlockElement::$rows[200]);
    $USER->id = 8;
    expectError('PANDA_BRAND_ACCESS_DENIED', function () { PandaBrandRest::preview(['name' => 'LG']); });
    $USER->id = 7; CIBlockProperty::$link = 99;
    expectError('PANDA_BRAND_SCHEMA_MISMATCH', function () { PandaBrandRest::preview(['name' => 'LG']); });
    CIBlockProperty::$link = 12; CIBlockElementRights::$denied = [100];
    expectError('PANDA_BRAND_DICTIONARY_NOT_FULLY_READABLE', function () { PandaBrandRest::preview(['name' => 'LG']); });
    CIBlockElementRights::$denied = []; $db->busy = true;
    $lg = PandaBrandRest::preview(['name' => 'LG']);
    expectError('PANDA_BRAND_BUSY', function () use ($lg) { PandaBrandRest::resolve(writePlan($lg)); });
    $db->busy = false; CIBlockElement::$failAdd = true;
    expectError('PANDA_BRAND_CREATE_FAILED', function () use ($lg) { PandaBrandRest::resolve(writePlan($lg)); });
    check(!$db->locked, 'failed add releases lock'); CIBlockElement::$failAdd = false;
    CIBlockElement::$badReadback = true;
    expectError('PANDA_BRAND_READBACK_FAILED', function () use ($lg) { PandaBrandRest::resolve(writePlan($lg)); });
    check(!$db->locked, 'failed readback releases lock'); CIBlockElement::$badReadback = false;
    $recover = PandaBrandRest::resolve(writePlan($lg));
    check(!$recover['created'] && CIBlockElement::$adds === 2, 'readback retry reuses committed brand');
    CIBlockElement::$rows[100]['NAME'] = 'Renamed';
    expectError('PANDA_BRAND_IDENTITY_CHANGED', function () { PandaBrandRest::preview(['name' => 'TCL']); });
    check(CIBlockElement::$adds === 2, 'no writes in rejection cases');
    echo $checks . " checks passed (Bitrix and database doubles)\n";
}

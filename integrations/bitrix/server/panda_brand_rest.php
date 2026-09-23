<?php
/** Authenticated REST extension. Load from local/php_interface/init.php only.
 * This file only declares a class: no request dispatch, bootstrap or registration.
 * init.php can load before B_PROLOG_INCLUDED; authorization runs inside callbacks.
 */

final class PandaBrandRest
{
    private static $config = [];

    public static function configure(array $config): void
    {
        self::$config = $config;
    }

    public static function methods(): array
    {
        return ['catalog' => [
            'panda.brand.preview' => ['callback' => [self::class, 'preview']],
            'panda.brand.resolve' => ['callback' => [self::class, 'resolve']],
        ]];
    }

    private static function fail(string $code): void
    {
        throw new \Bitrix\Rest\RestException($code, $code);
    }

    private static function authorize(bool $write): int
    {
        global $USER;
        $allowed = array_map('intval', self::$config['allowed_user_ids'] ?? []);
        if (!$USER || !$USER->IsAuthorized() || !in_array((int)$USER->GetID(), $allowed, true)) {
            self::fail('PANDA_BRAND_ACCESS_DENIED');
        }
        if (!\Bitrix\Main\Loader::includeModule('iblock') || !class_exists('Normalizer') || !function_exists('mb_strtolower')) {
            self::fail('PANDA_BRAND_RUNTIME_NOT_CONFIGURED');
        }
        $iblock = (int)(self::$config['brand_iblock_id'] ?? 0);
        $catalog = (int)(self::$config['catalog_iblock_id'] ?? 0);
        $property = (int)(self::$config['brand_property_id'] ?? 0);
        if ($iblock <= 0 || $catalog <= 0 || $property <= 0) {
            self::fail('PANDA_BRAND_NOT_CONFIGURED');
        }
        $row = \CIBlockProperty::GetByID($property, $catalog)->Fetch();
        if (!$row || (int)$row['IBLOCK_ID'] !== $catalog || $row['CODE'] !== 'BRAND'
            || $row['PROPERTY_TYPE'] !== 'E' || (int)$row['LINK_IBLOCK_ID'] !== $iblock) {
            self::fail('PANDA_BRAND_SCHEMA_MISMATCH');
        }
        // Full visibility is necessary: hidden elements must never become false "absent" results.
        // Check modern permissions, including installations using extended iblock rights.
        if (!\CIBlockSectionRights::UserHasRightTo($iblock, 0, 'section_read')
            || !\CIBlockElementRights::UserHasRightTo($iblock, 0, 'element_read')
            || ($write && !\CIBlockSectionRights::UserHasRightTo($iblock, 0, 'section_element_bind'))) {
            self::fail('PANDA_BRAND_ACCESS_DENIED');
        }
        return $iblock;
    }

    public static function normalize(string $value): string
    {
        $value = \Normalizer::normalize($value, \Normalizer::FORM_KC);
        if ($value === false) { self::fail('PANDA_BRAND_INVALID_NAME'); }
        $value = preg_replace('/[\p{Z}\s]+/u', ' ', $value);
        return mb_strtolower(trim($value), 'UTF-8');
    }

    private static function name(array $params): string
    {
        if (!isset($params['name']) || !is_string($params['name'])
            || !mb_check_encoding($params['name'], 'UTF-8')
            || preg_match('/[\p{Cc}\p{Cf}]/u', $params['name'])) {
            self::fail('PANDA_BRAND_INVALID_NAME');
        }
        $name = trim(preg_replace('/[\p{Z}\s]+/u', ' ', \Normalizer::normalize($params['name'], \Normalizer::FORM_KC)));
        if ($name === '' || mb_strlen($name, 'UTF-8') > 255) { self::fail('PANDA_BRAND_INVALID_NAME'); }
        return $name;
    }

    private static function code(string $name): string
    {
        $code = \CUtil::translit(self::normalize($name), 'ru', [
            'replace_space' => '-', 'replace_other' => '-', 'change_case' => 'L', 'max_len' => 180,
        ]);
        return $code !== '' ? $code : 'brand-' . substr(hash('sha256', self::normalize($name)), 0, 32);
    }

    private static function plan(int $iblock, string $name): array
    {
        $key = self::normalize($name);
        $code = self::code($name);
        $xml = 'panda-brand-v1:' . hash('sha256', $key);
        // Include inactive elements and inspect the complete bounded list; never infer absence
        // from one REST page or permissions-filtered search. Oversized dictionaries fail closed.
        $rows = \CIBlockElement::GetList(['ID' => 'ASC'], ['IBLOCK_ID' => $iblock], false,
            ['nTopCount' => 10001], ['ID', 'IBLOCK_ID', 'NAME', 'CODE', 'XML_ID']);
        $matches = [];
        $count = 0;
        while ($row = $rows->Fetch()) {
            if (++$count > 10000) { self::fail('PANDA_BRAND_DICTIONARY_LIMIT'); }
            if (!\CIBlockElementRights::UserHasRightTo($iblock, (int)$row['ID'], 'element_read')) {
                self::fail('PANDA_BRAND_DICTIONARY_NOT_FULLY_READABLE');
            }
            if ((string)$row['XML_ID'] === $xml && self::normalize((string)$row['NAME']) !== $key) {
                self::fail('PANDA_BRAND_IDENTITY_CHANGED');
            }
            if (self::normalize((string)$row['NAME']) === $key
                || self::normalize((string)$row['CODE']) === self::normalize($code)
                || self::normalize((string)$row['CODE']) === $key) {
                $matches[(string)$row['ID']] = $row;
            }
        }
        if (count($matches) > 1) { self::fail('PANDA_BRAND_AMBIGUOUS'); }
        $match = $matches ? reset($matches) : null;
        return ['name' => $name, 'code' => $match ? (string)$match['CODE'] : $code,
            'iblock_id' => $iblock, 'brand_id' => $match ? (string)$match['ID'] : null,
            'action' => $match ? 'existing' : 'create'];
    }

    public static function preview($params, $start = 0, $server = null): array
    {
        $iblock = self::authorize(false);
        if (!is_array($params)) { self::fail('PANDA_BRAND_INVALID_PLAN'); }
        $name = self::name($params);
        $plan = self::plan($iblock, $name);
        if (!empty($params['expected_id']) && (string)$params['expected_id'] !== (string)$plan['brand_id']) {
            self::fail('PANDA_BRAND_ID_MISMATCH');
        }
        return $plan;
    }

    public static function resolve($params, $start = 0, $server = null): array
    {
        $iblock = self::authorize(true);
        if (!is_array($params)) { self::fail('PANDA_BRAND_INVALID_PLAN'); }
        $name = self::name($params);
        if ((int)($params['iblock_id'] ?? 0) !== $iblock
            || !in_array($params['action'] ?? '', ['existing', 'create'], true)
            || !is_string($params['code'] ?? null)
            || !is_string($params['idempotency_key'] ?? null)
            || !preg_match('/^[A-Za-z0-9:._-]{1,200}$/D', $params['idempotency_key'])) {
            self::fail('PANDA_BRAND_INVALID_PLAN');
        }
        if ($params['action'] === 'existing' && !preg_match('/^[1-9][0-9]*$/D', (string)($params['brand_id'] ?? ''))) {
            self::fail('PANDA_BRAND_INVALID_PLAN');
        }
        if ($params['action'] === 'create' && !empty($params['brand_id'])) { self::fail('PANDA_BRAND_INVALID_PLAN'); }
        $db = \Bitrix\Main\Application::getConnection();
        // Serialize this dictionary across PHP workers and application servers sharing MySQL.
        // Lock the dictionary, not a request key: distinct products still cannot create twins.
        $lock = 'panda-brand-iblock-' . $iblock;
        $locked = $db->query("SELECT GET_LOCK('" . $lock . "', 5) AS L")->fetch();
        if (!$locked || (int)$locked['L'] !== 1) { self::fail('PANDA_BRAND_BUSY'); }
        try {
            $plan = self::plan($iblock, $name);
            if ($params['action'] === 'existing'
                && ($plan['action'] !== 'existing' || (string)$params['brand_id'] !== $plan['brand_id'])) {
                self::fail('PANDA_BRAND_PLAN_CHANGED');
            }
            if ($plan['code'] !== $params['code']) { self::fail('PANDA_BRAND_PLAN_CHANGED'); }
            if ($plan['action'] === 'existing') { return $plan + ['created' => false]; }
            if (!\CIBlockElementRights::UserHasRightTo($iblock, 0, 'element_edit')) {
                self::fail('PANDA_BRAND_ACCESS_DENIED');
            }
            $element = new \CIBlockElement();
            // Durable identity independent of the product, request and process. After a lost
            // response, the next locked lookup finds the committed brand instead of adding again.
            $xml = 'panda-brand-v1:' . hash('sha256', self::normalize($name));
            $id = $element->Add(['IBLOCK_ID' => $iblock, 'NAME' => $name, 'CODE' => $plan['code'],
                'XML_ID' => $xml, 'ACTIVE' => 'Y']);
            if (!$id) { self::fail('PANDA_BRAND_CREATE_FAILED'); }
            $row = \CIBlockElement::GetByID((int)$id)->Fetch();
            if (!$row || (int)$row['IBLOCK_ID'] !== $iblock || self::normalize((string)$row['NAME']) !== self::normalize($name)
                || (string)$row['CODE'] !== $plan['code'] || (string)$row['XML_ID'] !== $xml) {
                self::fail('PANDA_BRAND_READBACK_FAILED');
            }
            $verified = self::plan($iblock, $name);
            if ($verified['brand_id'] !== (string)$id) { self::fail('PANDA_BRAND_READBACK_FAILED'); }
            return $verified + ['created' => true];
        } finally {
            $db->query("SELECT RELEASE_LOCK('" . $lock . "')");
        }
    }
}

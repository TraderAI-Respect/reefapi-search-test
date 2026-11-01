"""Offline-only regression tests; extract report code without executing its main()."""
import pathlib
import textwrap
import unittest

ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW = ROOT / '.github/workflows/asic-full-scan.yml'
text = WORKFLOW.read_text(encoding='utf-8')
block = text.split('      - name: Build matching ASIC table', 1)[1]
block = block.split("          python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0]
report = {'__name__': 'offline_general_report'}
exec(compile(textwrap.dedent(block), str(WORKFLOW) + ':report', 'exec'), report)


def ad(title, description='', ad_id='12345678'):
    return {'ad_id': ad_id, 'title': title, 'description_snippet': description,
            'url': 'https://www.avito.ru/moskva/oborudovanie_dlya_biznesa/asic_' + ad_id,
            'city': 'Москва', 'price_text': '10 000 ₽'}


class GeneralNegativeFilterTests(unittest.TestCase):
    def test_brand_free_uploaded_negative_intents(self):
        self.assertIn('negative_title_reason', report)
        intents = ['Выкуп', 'Приём', 'Скупка', 'Обучение ремонту', 'Ремонт',
                   'Майнинг-отель', 'Хостинг', 'Размещение', 'Кулер', 'Шлейф',
                   'Кабель питания', 'Жидкостный охладитель', 'Блок питания',
                   'Электронные компоненты', 'Чип']
        for intent in intents:
            for model in ['Innosilicon A10 Pro', 'Innosilicon A11 Pro',
                          'Antminer S19 95T', 'Whatsminer M31S 74T', 'Avalon 841']:
                self.assertIsNotNone(report['negative_title_reason'](intent + ' ' + model, ''), intent)

    def test_whole_faulty_miners_and_stock_rules_preserved(self):
        cases = [('Innosilicon A10 Pro под ремонт', 'Продам свой асик. 1 шт.', 1),
                 ('Innosilicon A11 Pro после ремонта', 'Продам свой майнер. 1 шт.', 1),
                 ('Antminer S19 95T под ремонт', 'Продаются 15 шт.', 15),
                 ('Whatsminer M31S 74T неисправные', 'В наличии 20 шт.', 20),
                 ('Avalon 841 на запчасти', 'В наличии 20 шт.', 20)]
        for title, desc, qty in cases:
            rows, pending = report['classify'](ad(title, desc))
            self.assertEqual(1, len(rows), (title, rows, pending))
            self.assertEqual(qty, rows[0]['quantity_min'])

    def test_bundled_psu_is_not_separate_parts_lot(self):
        for title, desc in [('Antminer S19 95T с блоками питания', 'В наличии 15 шт. с блоками питания.'),
                            ('Antminer S19 95T в комплекте с БП', '15 аппаратов, каждый с БП.'),
                            ('Innosilicon A10 Pro с блоком питания', 'Продам свой асик.'),
                            ('Innosilicon A11 Pro с кабелем питания', 'Продам свой майнер.')]:
            rows, pending = report['classify'](ad(title, desc))
            self.assertEqual(1, len(rows), (title, rows, pending))

    def test_services_and_separate_parts_not_hidden_by_context(self):
        for title in ['Ремонт Antminer S19 95T после ремонта',
                      'Блок питания Innosilicon A11 Pro после ремонта',
                      'Кабель Innosilicon A10 Pro с блоком питания',
                      '15 блоков питания для Antminer S19 95T',
                      'Электронные компоненты для Innosilicon A11 Pro']:
            self.assertEqual(([], []), report['classify'](ad(title, 'В наличии 20 шт.')), title)

    def test_bundled_accessory_count_cannot_inflate_miner_stock(self):
        rows, _ = report['classify'](ad('Antminer S19 95T', '15 асиков с БП 20 шт.'))
        self.assertEqual(15, rows[0]['quantity_min'])
        rows, _ = report['classify'](ad('Antminer S19 95T', '14 асиков с БП 20 шт.'))
        self.assertEqual([], rows)

    def test_context_exceptions_do_not_admit_repair_intake_or_extenders(self):
        for title, desc in [
                ('Принимаю на ремонт Innosilicon A11 Pro', 'Ремонтируем асики быстро и качественно. Диагностика бесплатно.'),
                ('Запись на ремонт Innosilicon A11 Pro', 'Ремонтируем асики быстро и качественно.'),
                ('Удлинитель с кабелем питания для Innosilicon A11 Pro', 'Новый, 1 шт.'),
                ('Принимаю Innosilicon A11 Pro на ремонт', 'Ремонтируем асики быстро и качественно.'),
                ('Innosilicon A11 Pro — запись на ремонт', 'Ремонтируем асики быстро и качественно.'),
                ('На ремонт Innosilicon A11 Pro принимаем', 'Ремонтируем асики быстро и качественно.'),
                ('Удлинитель для Innosilicon A11 Pro с кабелем питания', 'Новый, 1 шт.'),
                ('Возьму Innosilicon A11 Pro на ремонт', 'Услуги мастера. Диагностика бесплатно.'),
                ('Принимается Innosilicon A11 Pro на ремонт', 'Услуги мастера. Диагностика бесплатно.'),
                ('Беру Innosilicon A11 Pro под ремонт', 'Услуги мастера. Диагностика бесплатно.'),
                ('На ремонт Innosilicon A11 Pro принимается', 'Услуги мастера. Диагностика бесплатно.')]:
            self.assertEqual(([], []), report['classify'](ad(title, desc)), title)

    def test_minimum_quantities_and_model_rules_unchanged(self):
        for title, desc in [('Antminer S19 95T', '14 шт.'),
                            ('Whatsminer M31S 74T', '19 шт.'),
                            ('Avalon 841', '19 шт.'),
                            ('Antminer S19 XP', '100 шт.')]:
            rows, _ = report['classify'](ad(title, desc))
            self.assertEqual([], rows, title)

    def test_ambiguous_corpus_goes_only_to_review(self):
        rows, pending = report['classify'](ad('Корпус Asic innosilicon a11 pro',
            'Пол года был включён в розетку. Идеальное состояние. Мой личный, стоял в квартире.'))
        self.assertEqual([], rows)
        self.assertEqual(1, len(pending))
        self.assertIn('комплектность', pending[0]['reason'])
        self.assertEqual(([], []), report['classify'](ad('Корпус Innosilicon A11 Pro', 'Пустой корпус без плат.')))

    def test_source_url_validation_unchanged(self):
        row = ad('Innosilicon A10 Pro', '1 шт.'); row['url'] = 'https://example.com/asic_12345678'
        self.assertEqual(([], []), report['classify'](row))


if __name__ == '__main__':
    unittest.main()

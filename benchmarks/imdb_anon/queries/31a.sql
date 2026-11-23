SELECT MIN(a07.c04) AS out_01,
       MIN(a08.c04) AS out_02,
       MIN(a10.c02) AS out_03,
       MIN(a11.c02) AS out_04
FROM
    t03 AS a01,
    t06 AS a02,
    t09 AS a03,
    t09 AS a04,
    t10 AS a05,
    t13 AS a06,
    t14 AS a07,
    t15 AS a08,
    t16 AS a09,
    t18 AS a10,
    t21 AS a11
WHERE a01.c05 IN ('(writer)',
                  '(head writer)',
                  '(written by)',
                  '(story)',
                  '(story editor)')
  AND a02.c02 LIKE 'Lionsgate%'
  AND a03.c02 = 'genres'
  AND a04.c02 = 'votes'
  AND a05.c02 IN ('murder',
                    'violence',
                    'blood',
                    'gore',
                    'death',
                    'female-nudity',
                    'hospital')
  AND a07.c04 IN ('Horror',
                  'Thriller')
  AND a10.c05 = 'm'
  AND a11.c01 = a07.c02
  AND a11.c01 = a08.c02
  AND a11.c01 = a01.c03
  AND a11.c01 = a09.c02
  AND a11.c01 = a06.c02
  AND a01.c03 = a07.c02
  AND a01.c03 = a08.c02
  AND a01.c03 = a09.c02
  AND a01.c03 = a06.c02
  AND a07.c02 = a08.c02
  AND a07.c02 = a09.c02
  AND a07.c02 = a06.c02
  AND a08.c02 = a09.c02
  AND a08.c02 = a06.c02
  AND a09.c02 = a06.c02
  AND a10.c01 = a01.c02
  AND a03.c01 = a07.c03
  AND a04.c01 = a08.c03
  AND a05.c01 = a09.c03
  AND a02.c01 = a06.c03;


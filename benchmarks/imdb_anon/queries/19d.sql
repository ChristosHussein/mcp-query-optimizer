SELECT MIN(a08.c02) AS out_01,
       MIN(a10.c02) AS out_02
FROM
    t01 AS a01,
    t04 AS a02,
    t03 AS a03,
    t06 AS a04,
    t09 AS a05,
    t13 AS a06,
    t14 AS a07,
    t18 AS a08,
    t20 AS a09,
    t21 AS a10
WHERE a03.c05 IN ('(voice)',
                  '(voice: Japanese version)',
                  '(voice) (uncredited)',
                  '(voice: English version)')
  AND a04.c03 ='[us]'
  AND a05.c02 = 'release dates'
  AND a08.c05 ='f'
  AND a09.c02 ='actress'
  AND a10.c05 > 2000
  AND a10.c01 = a07.c02
  AND a10.c01 = a06.c02
  AND a10.c01 = a03.c03
  AND a06.c02 = a03.c03
  AND a06.c02 = a07.c02
  AND a07.c02 = a03.c03
  AND a04.c01 = a06.c03
  AND a05.c01 = a07.c03
  AND a08.c01 = a03.c02
  AND a09.c01 = a03.c07
  AND a08.c01 = a01.c02
  AND a03.c02 = a01.c02
  AND a02.c01 = a03.c04;


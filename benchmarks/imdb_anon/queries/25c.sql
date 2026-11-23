SELECT MIN(a05.c04) AS out_01,
       MIN(a06.c04) AS out_02,
       MIN(a08.c02) AS out_03,
       MIN(a09.c02) AS out_04
FROM
    t03 AS a01,
    t09 AS a02,
    t09 AS a03,
    t10 AS a04,
    t14 AS a05,
    t15 AS a06,
    t16 AS a07,
    t18 AS a08,
    t21 AS a09
WHERE a01.c05 IN ('(writer)',
                  '(head writer)',
                  '(written by)',
                  '(story)',
                  '(story editor)')
  AND a02.c02 = 'genres'
  AND a03.c02 = 'votes'
  AND a04.c02 IN ('murder',
                    'violence',
                    'blood',
                    'gore',
                    'death',
                    'female-nudity',
                    'hospital')
  AND a05.c04 IN ('Horror',
                  'Action',
                  'Sci-Fi',
                  'Thriller',
                  'Crime',
                  'War')
  AND a08.c05 = 'm'
  AND a09.c01 = a05.c02
  AND a09.c01 = a06.c02
  AND a09.c01 = a01.c03
  AND a09.c01 = a07.c02
  AND a01.c03 = a05.c02
  AND a01.c03 = a06.c02
  AND a01.c03 = a07.c02
  AND a05.c02 = a06.c02
  AND a05.c02 = a07.c02
  AND a06.c02 = a07.c02
  AND a08.c01 = a01.c02
  AND a02.c01 = a05.c03
  AND a03.c01 = a06.c03
  AND a04.c01 = a07.c03;

